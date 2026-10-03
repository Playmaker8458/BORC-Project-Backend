"""คลังความรู้ Chatbot ของอาจารย์: ส่งต่อคำสั่งอัปโหลด/ดู/ลบ PDF ไปยัง ChatBot service

หน้า /ChatBotAdvisor ไม่เรียก ChatBot ตรงๆ — เรียก router นี้ (ต้องเป็นอาจารย์ที่ล็อกอิน) แล้ว backend ส่งต่อไป ChatBot_URL
พร้อม X-Internal-Secret เหมือนการแจ้งเตือน LINE (common/notify.py) เพื่อให้ ChatBot ไม่ต้องเปิดให้เบราว์เซอร์เรียกตรง
"""

import logging
import os
import re

import requests
from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from common import notify
from users.auth.authUser import get_current_advisor

logger = logging.getLogger(__name__)

router = APIRouter()

# ตรงกับ MAX_PDF_BYTES ของ ChatBot (/upload_pdf) — ตรวจก่อนส่งต่อเพื่อไม่ต้องเสียเวลาส่งไฟล์ที่จะถูกปฏิเสธ
MAX_PDF_BYTES = 50 * 1024 * 1024
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 30  # /files และ /delete_file
MAX_FILENAME_LENGTH = 255

# สถานะที่ ChatBot ตอบแล้วส่งต่อให้ผู้ใช้พร้อมข้อความ — อย่างอื่นตอบ 502 ข้อความกลาง (ไม่รั่ว detail ภายใน)
FORWARDED_STATUSES = {400, 404, 413}


def _upload_timeout() -> float:
    """CHATBOT_UPLOAD_TIMEOUT_SECONDS: เวลารอสูงสุดตอน ChatBot ประมวลผล PDF (ค่าที่ไม่ใช่ตัวเลขบวกถูกเมิน)"""
    try:
        value = float(os.getenv("CHATBOT_UPLOAD_TIMEOUT_SECONDS", ""))
    except ValueError:
        return DEFAULT_UPLOAD_TIMEOUT_SECONDS
    # NaN/inf/ติดลบ/ศูนย์ ใช้ไม่ได้
    return value if 0 < value < float("inf") else DEFAULT_UPLOAD_TIMEOUT_SECONDS


def _chatbot_url(path: str) -> str:
    base = (notify.CHATBOT_URL or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="ยังไม่ได้ตั้งค่าบริการ Chatbot")
    return f"{base}{path}"


def _call_chatbot(method: str, path: str, *, timeout, **kwargs) -> requests.Response:
    """ยิงไป ChatBot แล้วแปลงความล้มเหลวเป็น HTTPException: เชื่อมต่อไม่ได้ = 502, หมดเวลา = 504,
    400/404/413 ส่งต่อพร้อมข้อความ, สถานะผิดพลาดอื่น = 502"""
    url = _chatbot_url(path)
    try:
        resp = requests.request(
            method, url, headers=notify.CHATBOT_INTERNAL_HEADERS, timeout=(CONNECT_TIMEOUT_SECONDS, timeout), **kwargs
        )
    except requests.Timeout:
        logger.warning("Chatbot %s %s หมดเวลา", method, path)
        raise HTTPException(status_code=504, detail="Chatbot ตอบกลับช้าเกินไป กรุณาลองใหม่อีกครั้ง")
    except requests.RequestException:
        logger.exception("เรียก Chatbot %s %s ไม่สำเร็จ", method, path)
        raise HTTPException(status_code=502, detail="ไม่สามารถเชื่อมต่อ Chatbot ได้")

    if resp.status_code < 400:
        return resp

    logger.warning("Chatbot %s %s ตอบ HTTP %s: %s", method, path, resp.status_code, resp.text[:300])
    if resp.status_code in FORWARDED_STATUSES:
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise HTTPException(
            status_code=resp.status_code,
            detail=detail[:300] if isinstance(detail, str) and detail else "Chatbot ปฏิเสธคำขอ",
        )
    raise HTTPException(status_code=502, detail="Chatbot ทำงานผิดพลาด กรุณาลองใหม่อีกครั้ง")


def _file_basename(name: str) -> str:
    """ชื่อไฟล์ล้วน (ตัด path ของทั้ง / และ \\) — ChatBot เก็บ source อาจเป็น path เต็ม แต่ส่งรับเฉพาะชื่อ"""
    return re.split(r"[\\/]", name or "")[-1].strip()


def _fetch_files() -> list[dict]:
    """รายการเอกสารใน ChatBot ([{file_name, file_id}]) — ตรวจรูปแบบก่อนใช้ ถ้าผิดรูปแบบตอบ 502"""
    resp = _call_chatbot("GET", "/files", timeout=READ_TIMEOUT_SECONDS)
    try:
        files = resp.json()
    except ValueError:
        files = None
    valid = isinstance(files, list) and all(
        isinstance(f, dict) and isinstance(f.get("file_name"), str) and isinstance(f.get("file_id"), str) for f in files
    )
    if not valid:
        logger.error("Chatbot /files ตอบรูปแบบที่ไม่คาดคิด")
        raise HTTPException(status_code=502, detail="Chatbot ตอบกลับข้อมูลผิดรูปแบบ")
    return [{"file_name": f["file_name"], "file_id": f["file_id"]} for f in files]


@router.get("/files")
def list_files(request: Request):
    get_current_advisor(request)
    return _fetch_files()


@router.post("/upload")
def upload_pdf(request: Request, file: UploadFile = File(...)):
    get_current_advisor(request)

    filename = _file_basename(file.filename or "")
    if not filename.lower().endswith(".pdf") or len(filename) > MAX_FILENAME_LENGTH:
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF")

    # นับขนาดแบบอ่านทีละก้อน (UploadFile เก็บลง temp file เมื่อใหญ่ จึงไม่กินแรมทั้งไฟล์)
    stream = file.file
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="ไฟล์ PDF ต้องมีขนาดไม่เกิน 50 MB")
    if size == 0:
        raise HTTPException(status_code=400, detail="ไฟล์ว่างเปล่า")
    stream.seek(0)
    if stream.read(5) != b"%PDF-":
        raise HTTPException(status_code=400, detail="ไฟล์ไม่ใช่ PDF ที่ถูกต้อง")
    stream.seek(0)

    # ChatBot ไม่แทนที่ของเดิมเมื่ออัปโหลดชื่อเดิมซ้ำ (เพิ่มข้อมูลชุดที่สองต่อท้าย → บอทเห็นเนื้อหาซ้ำ) และการลบก็ลบทุกชุดที่ชื่อนี้
    # จึงไม่รับชื่อซ้ำ: ให้ลบไฟล์เดิมก่อนอย่างชัดเจน (เทียบแบบไม่สนตัวพิมพ์/path เหมือนที่หน้าเว็บรวมแถวซ้ำ)
    # ถ้าดึงรายการไม่ได้ก็ไม่อัปโหลด (fail closed) เพราะตรวจชื่อซ้ำไม่ได้
    wanted = filename.lower()
    if any(_file_basename(f["file_name"]).lower() == wanted for f in _fetch_files()):
        raise HTTPException(status_code=409, detail=f"มีไฟล์ชื่อ {filename} อยู่ในคลังแล้ว กรุณาลบไฟล์เดิมก่อนอัปโหลดใหม่")

    # ChatBot ประมวลผลจนเสร็จก่อนตอบ (สกัดข้อความ/รูป, embedding, สรุปด้วย LLM) จึงรอนานกว่าคำขออื่น
    _call_chatbot(
        "POST", "/upload_pdf", timeout=_upload_timeout(),
        files={"file": (filename, stream, "application/pdf")},
    )
    return {"message": f"{filename} uploaded and processed"}


@router.delete("/files")
def delete_file(request: Request, pdf_name: str):
    get_current_advisor(request)
    name = _file_basename(pdf_name)
    if not name or len(name) > MAX_FILENAME_LENGTH:
        raise HTTPException(status_code=400, detail="ชื่อไฟล์ไม่ถูกต้อง")
    _call_chatbot("DELETE", "/delete_file", timeout=READ_TIMEOUT_SECONDS, params={"pdf_name": name})
    return {"message": f"{name} deleted"}

"""ไฟล์แนบของคิวจอง (เอกสารงานวิจัยของนักศึกษา) — เก็บใน MongoDB ด้วย GridFS

เก็บในฐานข้อมูลเดียวกับระบบจอง จึงไม่ต้องพึ่งบริการภายนอกหรือดิสก์ของเซิร์ฟเวอร์ (ดิสก์ของ container หายเมื่อ
redeploy) และเอกสารวิจัยไม่ออกไปอยู่ที่อื่น GridFS แบ่งไฟล์เป็นก้อน 255KB เก็บในคอลเลกชัน
`attachments.files` / `attachments.chunks` แยกจากเอกสารคิว จึงไม่กระทบ query เดิม

ความเป็นส่วนตัว: ไม่มี URL สาธารณะ — ดาวน์โหลดได้ผ่าน endpoint ที่ตรวจสิทธิ์เท่านั้น (ManageQueueAdvisor)
ชื่อไฟล์ที่ผู้ใช้ส่งมาเก็บเป็นแค่ metadata ไม่ถูกใช้เป็นคีย์/path (คีย์คือ ObjectId ที่ระบบสร้าง)

หมายเหตุ: ไฟล์ใช้พื้นที่ฐานข้อมูลและ backup ร่วมกับข้อมูลอื่น (ไฟล์ละไม่เกิน 10MB) ตรวจโควตาของ Atlas ที่ใช้อยู่
"""

import logging
from dataclasses import dataclass
from urllib.parse import quote

import gridfs
from bson import ObjectId
from bson.errors import InvalidId

logger = logging.getLogger(__name__)

COLLECTION = "attachments"  # → attachments.files / attachments.chunks
STREAM_CHUNK_SIZE = 64 * 1024

# ไบต์แรกของไฟล์จริงแต่ละชนิด — Content-Type ที่เบราว์เซอร์ส่งมาปลอมได้ จึงเช็กเนื้อไฟล์ด้วย
_MAGIC_PREFIXES = {
    "application/pdf": (b"%PDF",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/msword": (b"\xd0\xcf\x11\xe0",),
}


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    contents: bytes


def content_matches_type(content_type: str, contents: bytes) -> bool:
    """เนื้อไฟล์ขึ้นต้นตรงกับชนิดที่ระบุ (PDF/DOCX/DOC) หรือไม่"""
    return any(contents.startswith(prefix) for prefix in _MAGIC_PREFIXES.get(content_type, ()))


def _bucket(db) -> gridfs.GridFS:
    return gridfs.GridFS(db, collection=COLLECTION)


def store_attachment(db, attachment: Attachment) -> str:
    """เก็บไฟล์ลง GridFS คืน id (ObjectId เป็น string) — ผู้เรียกเป็นคนแปลง error เป็น HTTP"""
    file_id = _bucket(db).put(
        attachment.contents,
        filename=attachment.filename,
        content_type=attachment.content_type,
    )
    return str(file_id)


def open_attachment(db, file_id):
    """เปิดไฟล์เพื่ออ่านเป็นสตรีม (GridOut: .length, .read(n), .close()) คืน None ถ้าไม่มี/id ผิดรูปแบบ"""
    try:
        oid = ObjectId(file_id)
    except (InvalidId, TypeError):
        return None
    try:
        return _bucket(db).get(oid)
    except gridfs.errors.NoFile:
        return None


def iter_file(grid_out, chunk_size: int = STREAM_CHUNK_SIZE):
    """วนอ่านไฟล์ทีละก้อน (ใช้กับ StreamingResponse — Starlette รัน iterator แบบ sync ใน threadpool)
    ปิดไฟล์เสมอ แม้ผู้ดาวน์โหลดตัดการเชื่อมต่อกลางทาง"""
    try:
        while chunk := grid_out.read(chunk_size):
            yield chunk
    finally:
        grid_out.close()


def content_disposition(filename: str) -> str:
    """header Content-Disposition แบบ attachment (บังคับดาวน์โหลด ไม่เปิดในเบราว์เซอร์)
    ชื่อไทยใช้ filename* (RFC 5987) ส่วน filename ธรรมดาเป็น ASCII สำรอง และตัดอักขระที่ทำให้ header เพี้ยนทิ้ง"""
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in filename) or "attachment"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def delete_attachments(db, file_ids) -> None:
    """ลบไฟล์แบบ best-effort (ล้มเหลวแค่ log — ไฟล์ค้างไม่ควรทำให้คำขอหลักล้ม)"""
    for file_id in file_ids:
        if not file_id:
            continue
        try:
            _bucket(db).delete(ObjectId(file_id))
        except Exception:
            logger.warning("ลบไฟล์แนบไม่สำเร็จ file_id=%s", file_id, exc_info=True)

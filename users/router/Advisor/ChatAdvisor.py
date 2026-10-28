import asyncio
import logging

logger = logging.getLogger(__name__)
import os
from typing import Annotated, Optional
from datetime import datetime, timezone

import certifi
import httpx
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi
from dotenv import load_dotenv
from pydantic import BaseModel, StrictInt, field_validator
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.exceptions import WebSocketException
from starlette import status as ws_status

from users.auth.authUser import ensure_user_role, verify_user_token, verify_ws_ticket, get_user_id
from common.url_safety import UnsafeUrl, validate_https_url
from common.timefmt import to_thai_iso
from common.booking_status import CHAT_STATUSES, COMPLETABLE_STATUSES
from common.chat_limits import ChatInvalid, check_rest_text, chat_rate_ok, parse_ws_text, retry_after_seconds, WS_POLICY_VIOLATION

from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL
from common.year_level import YEAR_LEVELS, parse_year_level
from common.chat_unread import get_advisor_unread_counts, mark_chat_read

load_dotenv(override=True)
router = APIRouter()

chatbot_uri = CHATBOT_URL
SERVER_CHATBOT_URL = f"{chatbot_uri}/NotifyChat/send_url/notification" #ยังไม่ได้ใช้ของจริง

# ⚠️ client/db/rooms เป็น module-level state ตัวเดียวที่ ChatStudent.py import ไปใช้ร่วมกัน
# (ไม่ได้สร้างซ้ำ) แต่ยัง global ต่อ process — ถ้า deploy แบบ multi-worker ในอนาคต ห้อง
# แชท (`rooms`) จะไม่ sync ข้าม worker (ต้องใช้ pub/sub ภายนอกเช่น Redis ซึ่งเป็นการเปลี่ยน
# stack — อยู่นอกขอบเขตของ fix นี้) ส่วน `client` ถูกปิดอย่างถูกต้องใน main.py lifespan แล้ว
def make_chat_client(uri: str | None):
    """AsyncMongoClient ของแชท — ใช้ CA bundle ของ certifi เหมือน client หลัก (common/mongodb_atlas.py)
    บาง Docker base image (เช่น python-slim) มี CA ระบบไม่ครบ ทำให้ต่อ Atlas ไม่ผ่าน (TLSV1_ALERT_INTERNAL_ERROR)
    เดิม client นี้ไม่ใส่ tlsCAFile จึงแชทอาจต่อ DB ไม่ได้ทั้งที่ส่วนอื่นของระบบต่อได้"""
    return AsyncMongoClient(uri, server_api=ServerApi("1"), tlsCAFile=certifi.where())


client = make_chat_client(os.getenv("MONGODB_ALART_CLIENT_URL"))
db = client["BORC"]


class ChatMessageBody(BaseModel):
    student_id: str
    text: str


class ChatAppointmentBody(BaseModel):
    student_id: str
    url: str


class ChatBroadcastBody(BaseModel):
    url: str
    # None = นักศึกษาทุกคนในรายการ; 1-4 = เฉพาะนักศึกษาที่ระบุชั้นปีนั้น (UserProfile.YearLevel)
    year_level: Optional[StrictInt] = None

    @field_validator("year_level", mode="after")
    @classmethod
    def _year_level_must_be_offered(cls, value):
        if value is not None and value not in YEAR_LEVELS:
            raise ValueError("ชั้นปีต้องเป็น 1 ถึง %d" % max(YEAR_LEVELS))
        return value


# ผู้รับการส่งลิงก์กลุ่ม = คิวที่หน้า "คิวนัดหมายที่อนุมัติ" แสดงจริง คือ Approved และ InProgress เท่านั้น
# (ตรงกับ CHATTABLE_STATUSES ฝั่ง frontend: src/lib/api/chat.ts) — ไม่รวม Pending (ยังไม่อนุมัติ), Rescheduled, Completed
# ทั้งที่ CHAT_STATUSES (สิทธิ์แชท) กว้างกว่านั้น
BROADCAST_STATUSES = COMPLETABLE_STATUSES  # ["Approved", "InProgress"]

# จำนวนคิวสูงสุดที่รายการ "คิวนัดหมายที่อนุมัติ" แสดง — การส่งทั้งกลุ่มใช้ชุดเดียวกัน (= คนในรายการที่อาจารย์เห็น)
APPROVED_QUEUE_LIMIT = 100
# ส่งแจ้งเตือน LINE พร้อมกันได้กี่รายการ (ไม่ยิงทีเดียวหมดเพื่อไม่ถล่มบริการแชทบอท)
LINE_PUSH_CONCURRENCY = 5


async def ensure_advisor_has_student(advisor_id: str, student_id: str) -> None:
    """อาจารย์ส่งข้อความ/ลิงก์ได้เฉพาะนักศึกษาที่มีคิวร่วมกัน (สถานะเดียวกับที่หน้าแชทและ WebSocket ใช้)

    เดิม REST endpoint ไม่ตรวจข้อนี้ ทำให้อาจารย์ส่งข้อความเข้าห้องแชทของนักศึกษาคนใดก็ได้
    และสั่งให้ ChatBot ส่งลิงก์ไปที่ LINE user ใดก็ได้
    """
    booking = await db["BookingOnline"].find_one({
        "UserId": student_id, "AdvisorId": advisor_id, "Status": {"$in": CHAT_STATUSES},
    })
    if not booking:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ส่งข้อความถึงนักศึกษาคนนี้ (ไม่มีคิวร่วมกัน)")


def _enforce_rate_limit(user_id: str) -> None:
    """จำกัดความถี่การส่งข้อความ/ลิงก์ต่อผู้ใช้ (นับรวมกับ WebSocket) เกินแล้วตอบ 429"""
    if not chat_rate_ok(user_id):
        raise HTTPException(
            status_code=429,
            detail="ส่งข้อความถี่เกินไป กรุณารอสักครู่",
            headers={"Retry-After": str(retry_after_seconds())},
        )


def _allowed_link_hosts() -> list[str]:
    """CHAT_LINK_ALLOWED_HOSTS (คั่นด้วยจุลภาค) จำกัดโดเมนของลิงก์นัดหมาย; ว่าง = ไม่จำกัดโดเมน"""
    raw = os.getenv("CHAT_LINK_ALLOWED_HOSTS") or ""
    return [h.strip() for h in raw.split(",") if h.strip()]


# ── ห้องแชท: เก็บ connection ที่เปิดอยู่ แยกตามคู่ (นักศึกษา, อาจารย์) ──────────────────────
# (ให้ ChatStudent.py import ไปใช้ตัวเดียวกัน) แยกตามคู่เพราะนักศึกษาหนึ่งคนอาจมีคิวกับอาจารย์
# หลายคนตามเวลา ถ้าใช้ student_id อย่างเดียว อาจารย์คนเก่าจะได้รับข้อความสดที่ส่งถึงอาจารย์คนปัจจุบัน
rooms: dict[tuple[str, str], list[WebSocket]] = {}


def room_key(student_id: str, advisor_id: str) -> tuple[str, str]:
    return (student_id, advisor_id)


def room_connect(key: tuple[str, str], ws: WebSocket):
    rooms.setdefault(key, []).append(ws)


def room_disconnect(key: tuple[str, str], ws: WebSocket):
    if key in rooms:
        if ws in rooms[key]:
            rooms[key].remove(ws)
        if not rooms[key]:
            del rooms[key]


async def room_broadcast(key: tuple[str, str], message: dict):
    for ws in list(rooms.get(key, [])):  # สำเนา: room_disconnect แก้ list ระหว่างวนได้
        try:
            await ws.send_json(message)
        except Exception:
            room_disconnect(key, ws)

# token
async def get_current_user_ws(websocket: WebSocket):
    """ยืนยันตัวตนของ WebSocket: ตั๋ว ?ticket= (จาก POST /authUser/ChatTicket) หรือ cookie session (ทางสำรอง)

    ถ้ามี ticket แต่ไม่ถูกต้อง → ปฏิเสธเลย ไม่ย้อนไปใช้ cookie (ไม่ให้ตั๋วเสียถูกมองข้ามเงียบๆ)"""
    try:
        ticket = websocket.query_params.get("ticket")
        if ticket is not None:
            return verify_ws_ticket(ticket)
        return verify_user_token(websocket)
    except HTTPException:
        raise WebSocketException(code=ws_status.WS_1008_POLICY_VIOLATION)


async def _year_levels_of(student_ids) -> dict[str, int | None]:
    ids = list({i for i in student_ids if i})
    if not ids:
        return {}
    profiles = await db["UserProfile"].find(
        {"userId": {"$in": ids}}, {"_id": 0, "userId": 1, "YearLevel": 1}
    ).to_list(length=None)
    # ในฐานข้อมูลเก็บเป็นข้อความ ("2") หรืออาจเป็นตัวเลข (แก้ด้วยมือ) — แปลงเป็นตัวเลขเสมอ; "" และค่าอื่น = ไม่ระบุ (None)
    return {p["userId"]: parse_year_level(p.get("YearLevel")) for p in profiles}


async def _broadcast_recipients(advisor_id: str, year_level: int | None) -> list[str]:
    """นักศึกษาในรายการ "คิวนัดหมายที่อนุมัติ" ของอาจารย์ (ไม่ซ้ำคน เรียงตามวันนัด) — คำนวณที่ backend เสมอ ไม่รับรายชื่อจาก client
    year_level ระบุ = เหลือเฉพาะคนที่มีชั้นปีนั้น"""
    bookings = await db["BookingOnline"].find(
        {"AdvisorId": advisor_id, "Status": {"$in": BROADCAST_STATUSES}}
    ).sort("Date", 1).to_list(length=APPROVED_QUEUE_LIMIT)

    student_ids = list(dict.fromkeys(b["UserId"] for b in bookings if b.get("UserId")))
    
    if year_level is None:
        return student_ids
    levels = await _year_levels_of(student_ids)
    
    return [s for s in student_ids if levels.get(s) == year_level]


async def _store_link_messages(advisor_id: str, recipients: list[str], url: str, now: datetime) -> None:
    """บันทึกลิงก์เป็นข้อความแชทชนิด link ในห้องของนักศึกษาแต่ละคน (คำสั่งเดียว)"""
    await db["ChatMessages"].insert_many([
        {"student_id": s, "advisor_id": advisor_id, "sender": "teacher", "type": "link", "text": url, "timestamp": now}
        for s in recipients
    ])


async def _deliver_to_open_rooms(advisor_id: str, recipients: list[str], url: str, now: datetime) -> None:
    """ส่งสดเข้าห้องแชทที่นักศึกษาเปิดอยู่ (ไล่ทีละห้อง)"""
    live = {"sender": "teacher", "type": "link", "text": url, "timestamp": to_thai_iso(now)}
    for s in recipients:
        await room_broadcast(room_key(s, advisor_id), live)


async def _push_line_link(http: httpx.AsyncClient, slots: asyncio.Semaphore, student_id: str, url: str) -> bool:
    """แจ้งเตือน LINE ถึงนักศึกษาหนึ่งคนผ่านแชทบอท; คืน True เมื่อสำเร็จ (สถานะต่ำกว่า 400), False เมื่อล้มเหลว"""
    async with slots:
        try:
            resp = await http.post(
                SERVER_CHATBOT_URL,
                json={"line_user_id": student_id, "url": url},
                timeout=5.0,
                headers=CHATBOT_INTERNAL_HEADERS,
            )
        except httpx.RequestError as exc:
            logger.warning(f"[WARN] แจ้งเตือน LINE ไม่สำเร็จ ({student_id}): {exc}")
            return False
        return getattr(resp, "status_code", 200) < 400


async def _notify_line_link(recipients: list[str], url: str) -> int:
    """แจ้งเตือน LINE ทุกคนพร้อมกันไม่เกิน LINE_PUSH_CONCURRENCY รายการ; คืนจำนวนที่แจ้งไม่สำเร็จ"""
    slots = asyncio.Semaphore(LINE_PUSH_CONCURRENCY)
    async with httpx.AsyncClient() as http:
        pushed = await asyncio.gather(*(_push_line_link(http, slots, s, url) for s in recipients))
    return pushed.count(False)


# ── REST: ของเดิม ──────────────────────────────────────────────────────────
@router.get("/AdvisorApprovedQueue")
async def get_advisor_queues_v2(request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)

        bookings = await db["BookingOnline"].find(
            {"AdvisorId": advisor_id, "Status": {"$in": CHAT_STATUSES}}
        ).sort("Date", 1).to_list(length=APPROVED_QUEUE_LIMIT)

        # ชั้นปีของนักศึกษา (ใช้กรอง/นับผู้รับในกล่องส่งลิงก์กลุ่ม); ไม่มีค่า (นักศึกษาเดิม) = None
        year_levels = await _year_levels_of([b.get("UserId") for b in bookings])
        for b in bookings:
            b["_id"] = str(b["_id"])
            b["YearLevel"] = year_levels.get(b.get("UserId"))

        return {"queues": bookings}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/chat/history/{student_id}")
async def get_chat_history(student_id: str, request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)

        messages = await db["ChatMessages"].find(
            {"student_id": student_id, "advisor_id": advisor_id}
        ).sort("timestamp", 1).to_list(length=500)

        return [{
            "sender": m.get("sender", "teacher"),
            "type": m.get("type", "text"),
            "text": m.get("text", ""),
            "timestamp": to_thai_iso(m["timestamp"]) if m.get("timestamp") else "",
        } for m in messages]
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── GET จำนวนข้อความค้างอ่าน (มุมอาจารย์) — badge ต่อรายชื่อนักศึกษาใน sidebar หน้าแชท ──
# คืนค่าได้แม้ไม่เคยเปิดห้องแชทกับนักศึกษาคนนั้นเลยในเซสชันนี้ (นับจาก ChatMessages ที่
# persist ไว้แล้ว ไม่ได้พึ่ง state ในหน่วยความจำของ WebSocket ที่ต่อเฉพาะห้องที่เปิดอยู่)
@router.get("/chat/unread/students")
async def get_advisor_chat_unread(request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)

        bookings = await db["BookingOnline"].find(
            {"AdvisorId": advisor_id, "Status": {"$in": CHAT_STATUSES}}
        ).to_list(length=100)
        student_ids = list({b.get("UserId") for b in bookings if b.get("UserId")})

        unread = await get_advisor_unread_counts(db, advisor_id=advisor_id, student_ids=student_ids)
        return {"unread": unread}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── POST มาร์คว่าอ่านแล้ว (มุมอาจารย์) — เรียกตอนเปิดห้องแชทกับนักศึกษาคนนี้
# ⚠️ ต้องอยู่หลัง /chat/read/mine ของ ChatStudent.py เสมอ (ดูหมายเหตุเรื่องลำดับ include_router
# ใน main.py — literal "mine" กับ path param {student_id} จำนวน segment เท่ากัน) ──
@router.post("/chat/read/{student_id}")
async def mark_advisor_chat_read(student_id: str, request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)
        await mark_chat_read(db, advisor_id, student_id)
        return {"message": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/chat/message")
async def send_chat_message(body: ChatMessageBody, request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)
        _enforce_rate_limit(advisor_id)
        await ensure_advisor_has_student(advisor_id, body.student_id)
        try:
            text = check_rest_text(body.text)
        except ChatInvalid as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        now = datetime.now(timezone.utc)

        await db["ChatMessages"].insert_one({
            "student_id": body.student_id, "advisor_id": advisor_id,
            "sender": "teacher", "type": "text", "text": text, "timestamp": now,
        })
        await room_broadcast(room_key(body.student_id, advisor_id), {
            "sender": "teacher", "type": "text", "text": text,
            "timestamp": to_thai_iso(now),
        })
        return {"message": "ส่งข้อความสำเร็จ"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/chat/appointment")
async def send_chat_appointment(body: ChatAppointmentBody, request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)
        _enforce_rate_limit(advisor_id)
        await ensure_advisor_has_student(advisor_id, body.student_id)
        try:
            url = validate_https_url(body.url, allowed_hosts=_allowed_link_hosts())
        except UnsafeUrl as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        now = datetime.now(timezone.utc)

        await db["ChatMessages"].insert_one({
            "student_id": body.student_id, "advisor_id": advisor_id,
            "sender": "teacher", "type": "link", "text": url, "timestamp": now,
        })
        await room_broadcast(room_key(body.student_id, advisor_id), {
            "sender": "teacher", "type": "link", "text": url,
            "timestamp": to_thai_iso(now),
        })

        async with httpx.AsyncClient() as http:
            try:
                await http.post(
                    SERVER_CHATBOT_URL,
                    json={"line_user_id": body.student_id, "url": url},
                    timeout=5.0,
                    headers=CHATBOT_INTERNAL_HEADERS,
                )
            except httpx.RequestError as exc:
                logger.warning(f"[WARN] Server 5000 ติดต่อไม่ได้: {exc}")

        return {"message": "ส่งลิงก์นัดหมายสำเร็จ"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/chat/appointment/broadcast")
async def broadcast_chat_appointment(body: ChatBroadcastBody, request: Request):
    """ส่งลิงก์นัดหมายถึงนักศึกษาทุกคนในรายการคิวที่อนุมัติ (กรองตามชั้นปีได้) — เหมือนส่งเดี่ยวทุกขั้นตอน:
    บันทึกเป็นข้อความแชทในห้องของแต่ละคน, ส่งสดเข้าห้องที่เปิดอยู่, แจ้งเตือน LINE"""
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)
        _enforce_rate_limit(advisor_id)
        try:
            url = validate_https_url(body.url, allowed_hosts=_allowed_link_hosts())
        except UnsafeUrl as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        recipients = await _broadcast_recipients(advisor_id, body.year_level)
        if not recipients:
            raise HTTPException(status_code=400, detail="ไม่มีนักศึกษาในรายการที่ตรงกับชั้นปีที่เลือก")

        now = datetime.now(timezone.utc)
        await _store_link_messages(advisor_id, recipients, url, now)
        await _deliver_to_open_rooms(advisor_id, recipients, url, now)
        line_failed = await _notify_line_link(recipients, url) # ส่งข้อมูลการแจ้งเตือนไปยังไลน์


        async with httpx.AsyncClient() as http:
            try:
                await http.post(
                    SERVER_CHATBOT_URL,
                    json={"line_user_id": recipients, "url": url},
                    timeout=5.0,
                    headers=CHATBOT_INTERNAL_HEADERS,
                )
            except httpx.RequestError as exc:
                logger.warning(f"[WARN] Server 5000 ติดต่อไม่ได้: {exc}")

        return {
            "message": "ส่งลิงก์นัดหมายสำเร็จ",
            "total": len(recipients),
            "line_failed": line_failed,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── WebSocket: ฝั่งอาจารย์ ───────────────────────────────────────────────────
@router.websocket("/chat/ws/advisor/{student_id}")
async def advisor_chat_ws(
    websocket: WebSocket,
    student_id: str,
    payload: Annotated[dict, Depends(get_current_user_ws)],
):
    advisor_id = get_user_id(payload)
    ensure_user_role(payload, "Advisor")

    booking = await db["BookingOnline"].find_one({
        "UserId": student_id, "AdvisorId": advisor_id, "Status": {"$in": CHAT_STATUSES},
    })
    if not booking:
        raise WebSocketException(code=ws_status.WS_1008_POLICY_VIOLATION)

    await websocket.accept()
    key = room_key(student_id, advisor_id)
    room_connect(key, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                text = parse_ws_text(raw)
            except ChatInvalid as exc:
                room_disconnect(key, websocket)
                await websocket.close(code=exc.code)
                return
            if text is None:
                continue
            if not chat_rate_ok(advisor_id):
                room_disconnect(key, websocket)
                await websocket.close(code=WS_POLICY_VIOLATION)
                return
            now = datetime.now(timezone.utc)

            await db["ChatMessages"].insert_one({
                "student_id": student_id, "advisor_id": advisor_id,
                "sender": "teacher", "type": "text", "text": text, "timestamp": now,
            })
            await room_broadcast(key, {
                "sender": "teacher", "type": "text", "text": text,
                "timestamp": to_thai_iso(now),
            })
    except WebSocketDisconnect:
        room_disconnect(key, websocket)
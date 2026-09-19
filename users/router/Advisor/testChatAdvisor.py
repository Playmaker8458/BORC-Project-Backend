import logging

logger = logging.getLogger(__name__)
import os
from typing import Annotated
from datetime import datetime, timezone

import httpx
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi
from dotenv import load_dotenv
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.exceptions import WebSocketException
from starlette import status as ws_status

from users.auth.authUser import ensure_user_role, verify_user_token, get_user_id

from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL

load_dotenv(override=True)
router = APIRouter()

ACTIVE_STATUSES = ["Pending", "Approved", "Rescheduled", "InProgress", "Completed"]

chatbot_uri = CHATBOT_URL
SERVER_CHATBOT_URL = f"{chatbot_uri}/NotifyChat/send_url/notification" #ยังไม่ได้ใช้ของจริง

# ⚠️ client/db/rooms เป็น module-level state ตัวเดียวที่ ChatStudent.py import ไปใช้ร่วมกัน
# (ไม่ได้สร้างซ้ำ) แต่ยัง global ต่อ process — ถ้า deploy แบบ multi-worker ในอนาคต ห้อง
# แชท (`rooms`) จะไม่ sync ข้าม worker (ต้องใช้ pub/sub ภายนอกเช่น Redis ซึ่งเป็นการเปลี่ยน
# stack — อยู่นอกขอบเขตของ fix นี้) ส่วน `client` ถูกปิดอย่างถูกต้องใน main.py lifespan แล้ว
client = AsyncMongoClient(os.getenv("MONGODB_ALART_CLIENT_URL"), server_api=ServerApi("1"))
db = client["BORC"]


class ChatMessageBody(BaseModel):
    student_id: str
    text: str


class ChatAppointmentBody(BaseModel):
    student_id: str
    url: str


# ── ห้องแชท: เก็บ connection ที่เปิดอยู่ แยกตาม student_id (ให้ student_chat.py import ไปใช้ตัวเดียวกัน) ──
rooms: dict[str, list[WebSocket]] = {}


def room_connect(student_id: str, ws: WebSocket):
    rooms.setdefault(student_id, []).append(ws)


def room_disconnect(student_id: str, ws: WebSocket):
    if student_id in rooms:
        if ws in rooms[student_id]:
            rooms[student_id].remove(ws)
        if not rooms[student_id]:
            del rooms[student_id]


async def room_broadcast(student_id: str, message: dict):
    for ws in rooms.get(student_id, []):
        try:
            await ws.send_json(message)
        except Exception:
            room_disconnect(student_id, ws)

# token
async def get_current_user_ws(websocket: WebSocket):
    try:
        return verify_user_token(websocket)
    except HTTPException:
        raise WebSocketException(code=ws_status.WS_1008_POLICY_VIOLATION)


# ── REST: ของเดิม ──────────────────────────────────────────────────────────
@router.get("/AdvisorApprovedQueue")
async def get_advisor_queues_v2(request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Advisor")
        advisor_id = get_user_id(payload)

        bookings = await db["BookingOnline"].find(
            {"AdvisorId": advisor_id, "Status": {"$in": ACTIVE_STATUSES}}
        ).sort("Date", 1).to_list(length=100)

        for b in bookings:
            b["_id"] = str(b["_id"])

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
            "timestamp": m["timestamp"].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if m.get("timestamp") else "",
        } for m in messages]
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
        now = datetime.now(timezone.utc)

        await db["ChatMessages"].insert_one({
            "student_id": body.student_id, "advisor_id": advisor_id,
            "sender": "teacher", "type": "text", "text": body.text, "timestamp": now,
        })
        await room_broadcast(body.student_id, {
            "sender": "teacher", "type": "text", "text": body.text,
            "timestamp": now.isoformat().replace("+00:00", "Z"),
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
        now = datetime.now(timezone.utc)

        await db["ChatMessages"].insert_one({
            "student_id": body.student_id, "advisor_id": advisor_id,
            "sender": "teacher", "type": "link", "text": body.url, "timestamp": now,
        })
        await room_broadcast(body.student_id, {
            "sender": "teacher", "type": "link", "text": body.url,
            "timestamp": now.isoformat().replace("+00:00", "Z"),
        })

        async with httpx.AsyncClient() as http:
            try:
                await http.post(
                    SERVER_CHATBOT_URL,
                    json={"line_user_id": body.student_id, "url": body.url},
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
        "UserId": student_id, "AdvisorId": advisor_id, "Status": {"$in": ACTIVE_STATUSES},
    })
    if not booking:
        raise WebSocketException(code=ws_status.WS_1008_POLICY_VIOLATION)

    await websocket.accept()
    room_connect(student_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            now = datetime.now(timezone.utc)

            await db["ChatMessages"].insert_one({
                "student_id": student_id, "advisor_id": advisor_id,
                "sender": "teacher", "type": "text", "text": data.get("text", ""), "timestamp": now,
            })
            await room_broadcast(student_id, {
                "sender": "teacher", "type": "text", "text": data.get("text", ""),
                "timestamp": now.isoformat().replace("+00:00", "Z"),
            })
    except WebSocketDisconnect:
        room_disconnect(student_id, websocket)

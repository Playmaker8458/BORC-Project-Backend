import logging

logger = logging.getLogger(__name__)
from typing import Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.exceptions import WebSocketException
from starlette import status as ws_status

from users.auth.authUser import ensure_user_role, verify_user_token, get_user_id
# ⚠️ ปรับ path import ให้ตรงกับตำแหน่งไฟล์ advisor_chat.py จริงในโปรเจกต์
from ..Advisor.testChatAdvisor import db, ACTIVE_STATUSES, room_connect, room_disconnect, room_broadcast, get_current_user_ws

router = APIRouter()


async def resolve_advisor_id_for_student(student_id: str) -> str:
    """หา advisor_id ของนักศึกษาคนนี้ จาก booking ที่ active อยู่ (token นักศึกษาไม่มี advisor_id ติดมา)

    คืนค่า "" ทั้งสองกรณี (ไม่มี booking active เลย / มี booking แต่ไม่มี AdvisorId)
    เพื่อไม่เปลี่ยนพฤติกรรมเดิมของผู้เรียก แต่กรณีหลัง log เป็น warning เพราะ
    booking ที่ active แล้วไม่มี AdvisorId ถือเป็นข้อมูลผิดปกติ ไม่ใช่เรื่องปกติแบบกรณีแรก
    """
    booking = await db["BookingOnline"].find_one(
        {"UserId": student_id, "Status": {"$in": ACTIVE_STATUSES}}
    )
    if booking is None:
        return ""

    advisor_id = booking.get("AdvisorId", "")
    if not advisor_id:
        logger.warning(
            "Active booking %s for student %s has no AdvisorId set",
            booking.get("_id"), student_id,
        )
    return advisor_id

#ดึงคิวนัดหมายของตัวเอง (มุมนักศึกษา) — mirror จาก /AdvisorApprovedQueue ของอาจารย์
@router.get("/StudentApprovedQueue")
async def get_my_approved_queue(request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Student")
        my_user_id = get_user_id(payload)
 
        bookings = await db["BookingOnline"].find(
            {"UserId": my_user_id, "Status": {"$in": ACTIVE_STATUSES}}
        ).sort("Date", 1).to_list(length=100)
 
        for b in bookings:
            b["_id"] = str(b["_id"])
 
        return {"queues": bookings}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

# ── GET ดึงประวัติแชท (มุมนักศึกษา) — ดึง student_id จาก token เลย ไม่ต้องรับเป็น path param ──
@router.get("/chat/history/mine")
async def get_my_chat_history(request: Request):
    try:
        payload = verify_user_token(request)
        ensure_user_role(payload, "Student")
        student_id = get_user_id(payload)

        advisor_id = await resolve_advisor_id_for_student(student_id)
        if not advisor_id:
            return []

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


# ── WebSocket: ฝั่งนักศึกษา (ส่งได้แค่ข้อความธรรมดา) — ดึง student_id จาก token เลย ──
@router.websocket("/chat/ws/student/{student_id}")
async def student_chat_ws(
    websocket: WebSocket,
    payload: Annotated[dict, Depends(get_current_user_ws)],
):
    student_id = get_user_id(payload)
    ensure_user_role(payload, "Student")

    advisor_id = await resolve_advisor_id_for_student(student_id)
    if not advisor_id:
        raise WebSocketException(code=ws_status.WS_1008_POLICY_VIOLATION)

    await websocket.accept()
    room_connect(student_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            now = datetime.now(timezone.utc)

            await db["ChatMessages"].insert_one({
                "student_id": student_id, "advisor_id": advisor_id,
                "sender": "student", "type": "text", "text": data.get("text", ""), "timestamp": now,
            })
            await room_broadcast(student_id, {
                "sender": "student", "type": "text", "text": data.get("text", ""),
                "timestamp": now.isoformat().replace("+00:00", "Z"),
            })
    except WebSocketDisconnect:
        room_disconnect(student_id, websocket)

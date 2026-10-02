import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone
from pydantic import BaseModel
from dotenv import load_dotenv
from common.slot_service import (
    ensure_slot_open_for_reschedule,
    get_reschedule_slot_options,
    is_within_advisor_cutoff_window,
    move_booking_to_slot,
    split_time_range,
)
from common.booking_status import ACTIVE_STATUSES
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.reschedule_flow import record_reschedule_history, resolve_same_day_mode


router = APIRouter()

load_dotenv(override=True)
chatbot_uri = CHATBOT_URL


class RescheduleBody(BaseModel):
    user_id   : str
    new_date  : str
    new_start : str
    new_end   : str
    new_label : str = ""
    reason    : str = ""
    mode      : str = "date_time"  # "date_time" | "time_only" (เลื่อนเฉพาะเวลาในวันเดิม)


# ─── GET /advisor-reschedule/BookingInfo?user_id=<studentUserId> ──────────────
@router.get("/BookingInfo")
def get_booking_info(user_id: str, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {
                "_id": 1, "UserId": 1, "StudentName": 1, "Advisor_Name": 1,
                "AdvisorId": 1, "Date": 1, "Time": 1, "TimeLabel": 1,
                "ResearchTopic": 1, "Status": 1,
                "RescheduledOnce"       : 1,
                "AdvisorRescheduledOnce": 1,
            }
        )

        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจองของนักศึกษา")

        if booking.get("AdvisorId", "") != advisor_id:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์จัดการคิวนี้")

        if booking["Status"] != "Approved":
            raise HTTPException(
                status_code=400,
                detail=f"เลื่อนคิวได้เฉพาะสถานะ Approved เท่านั้น (ปัจจุบัน: {booking['Status']})"
            )

        if booking.get("AdvisorRescheduledOnce", False):
            raise HTTPException(
                status_code=400,
                detail="อาจารย์ใช้สิทธิ์เลื่อนคิวนี้ไปแล้ว 1 ครั้ง"
            )

        time_str = booking.get("Time", "")
        start_time, end_time = split_time_range(time_str)

        if start_time and is_within_advisor_cutoff_window(booking.get("Date", ""), start_time):
            raise HTTPException(
                status_code=400,
                detail="อยู่ในช่วงเวลานัดหมาย ไม่สามารถเลื่อนคิวได้ (1 ชั่วโมงก่อน ถึง 1 ชั่วโมงหลังเวลาเริ่มนัด)"
            )

        return {
            "booking": {
                "bookingId"    : str(booking["_id"]),
                "userId"       : booking.get("UserId", ""),
                "studentName"  : booking.get("StudentName", ""),
                "advisorName"  : booking.get("Advisor_Name", ""),
                "advisorId"    : booking.get("AdvisorId", ""),
                "date"         : booking.get("Date", ""),
                "startTime"    : start_time,
                "endTime"      : end_time,
                "time"         : time_str,
                "timeLabel"    : booking.get("TimeLabel", ""),
                "researchTopic": booking.get("ResearchTopic", ""),
                "status"       : booking.get("Status", ""),
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[BookingInfo] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /advisor-reschedule/AvailableSlots?user_id=<studentUserId> ───────────
@router.get("/AvailableSlots")
def get_available_slots(user_id: str, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {"AdvisorId": 1, "Date": 1, "Time": 1}
        )

        if not booking:
            return {"dates": {}, "same_day": []}

        if booking.get("AdvisorId", "") != advisor_id:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์จัดการคิวนี้")

        old_start, _ = split_time_range(booking.get("Time", ""))
        return get_reschedule_slot_options(db, advisor_id, booking.get("Date", ""), old_start)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[AvailableSlots] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


def _load_reschedulable_booking(db, advisor_id: str, student_id: str) -> tuple[dict, str, str]:
    """ดึงคิวของนักศึกษาที่อาจารย์เลื่อนได้ พร้อมเวลาเดิม; raise HTTPException ถ้าเลื่อนไม่ได้"""
    booking = db["BookingOnline"].find_one(
        {"UserId": student_id, "Status": {"$in": ACTIVE_STATUSES}}
    )
    if not booking:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

    if booking.get("AdvisorId", "") != advisor_id:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์จัดการคิวนี้")

    if booking["Status"] != "Approved":
        raise HTTPException(
            status_code=400,
            detail=f"เลื่อนได้เฉพาะสถานะ Approved เท่านั้น (ปัจจุบัน: {booking['Status']})"
        )

    # เช็คสิทธิ์อาจารย์จาก AdvisorRescheduledOnce — ไม่แตะ RescheduledOnce ของนักศึกษา
    if booking.get("AdvisorRescheduledOnce", False):
        raise HTTPException(
            status_code=400,
            detail="ไม่สามารถเลื่อนคิวได้อีก เนื่องจากอาจารย์เลื่อนคิวนี้ไปแล้ว 1 ครั้ง"
        )

    old_start, old_end = split_time_range(booking.get("Time", ""))
    if old_start and is_within_advisor_cutoff_window(booking.get("Date", ""), old_start):
        raise HTTPException(
            status_code=400,
            detail="ไม่สามารถเลื่อนคิวได้ เนื่องจากอยู่ในช่วงเวลานัดหมาย (1 ชั่วโมงก่อน ถึง 1 ชั่วโมงหลังเวลาเริ่มนัด)"
        )

    return booking, old_start, old_end


# ─── PUT /advisor-reschedule/RescheduleBooking ────────────────────────────────
@router.put("/RescheduleBooking")
def reschedule_booking(request: Request, body: RescheduleBody, background_tasks: BackgroundTasks):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        db = Connect_MongoDB()["BORC"]
        booking, old_start, old_end = _load_reschedulable_booking(db, advisor_id, body.user_id)

        same_day = resolve_same_day_mode(
            body.mode, body.new_date, body.new_start, booking.get("Date", ""), old_start
        )

        ensure_slot_open_for_reschedule(
            db, advisor_id, body.new_date, body.new_start, body.new_end, same_day=same_day
        )

        now = datetime.now(timezone.utc)
        if not move_booking_to_slot(
            db, booking, advisor_id,
            body.new_date, body.new_start, body.new_end, old_start, old_end,
            extra_fields={"AdvisorRescheduledOnce": True},  # ไม่แตะ RescheduledOnce ของนักศึกษา
            now=now,
        ):
            raise HTTPException(status_code=409, detail="สถานะคิวเปลี่ยนไปแล้ว กรุณารีเฟรชหน้าแล้วลองใหม่อีกครั้ง")

        # แจ้งนักศึกษาว่าอาจารย์เลื่อนคิว (background — ไม่บล็อก event loop)
        student_id = booking.get("UserId", "")
        student_name = booking.get("StudentName", "")
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyQueueStudent/RecheduleStudent",
            {
                "UserId"     : student_id,
                "StudentName": student_name,
                "Date"       : body.new_date,
                "Time"       : f"{body.new_start}-{body.new_end}",
                "Status"     : "Rescheduled"
            },
            CHATBOT_INTERNAL_HEADERS,
        )

        record_reschedule_history(
            db,
            booking=booking,
            actor_id=advisor_id,
            actor_role="Advisor",
            advisor_id=advisor_id,
            advisor_name=booking.get("Advisor_Name", ""),
            student_id=student_id,
            student_name=student_name,
            new_date=body.new_date,
            new_start=body.new_start,
            new_end=body.new_end,
            new_label=body.new_label,
            reason=body.reason,
            old_start=old_start,
            old_end=old_end,
            now=now,
        )

        return {"message": "เลื่อนคิวสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[RescheduleBooking] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

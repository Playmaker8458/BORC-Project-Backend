import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Form, Depends
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone, timedelta
import requests as req  # ไม่ได้เรียกตรงนี้แล้ว (ใช้ common.notify แทน) แต่คงไว้เพราะ
                         # tests/test_security_fixes.py เข้าถึง module.req โดยตรง
from dotenv import load_dotenv
import os
from common.slot_service import (
    ensure_slot_open_for_reschedule,
    get_reschedule_available_dates,
    is_within_cutoff,
    move_booking_to_slot,
    split_time_range,
)
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.queue_history import log_queue_management_history


router = APIRouter()

load_dotenv(override=True)
chatbot_uri = CHATBOT_URL

ACTIVE_STATUSES = ["Pending", "Approved", "InProgress", "Rescheduled"]


class RescheduleForm:
    def __init__(
        self,
        new_date  : str = Form(...),
        new_start : str = Form(...),
        new_end   : str = Form(...),
        new_label : str = Form(""),
        reason    : str = Form(...)
    ):
        self.new_date  = new_date
        self.new_start = new_start
        self.new_end   = new_end
        self.new_label = new_label
        self.reason    = reason


# ---------------------------------------------------------------------------
# GET /reschedule/BookingInfo
# ---------------------------------------------------------------------------
@router.get("/BookingInfo")
def get_booking_info(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)

        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {"_id": 0}
        )

        if not booking:
            return {"booking": None}

        time_str   = booking.get("Time", "")
        time_parts = [t.strip() for t in time_str.split("-")] if "-" in time_str else []
        start_time = time_parts[0] if len(time_parts) == 2 else ""
        end_time   = time_parts[1] if len(time_parts) == 2 else ""

        within_cutoff    = is_within_cutoff(booking.get("Date", ""), start_time) if start_time else False
        rescheduled_once = booking.get("RescheduledOnce", False)  # ✅ เปลี่ยนจาก postpone_count

        can_reschedule = (
            booking.get("Status") == "Approved" and
            not rescheduled_once and               # ✅ เปลี่ยนจาก postpone_count == 0
            not within_cutoff
        )

        return {
            "booking": {
                "Advisor_Name"   : booking.get("Advisor_Name", ""),
                "AdvisorId"      : booking.get("AdvisorId", ""),
                "Date"           : booking.get("Date", ""),
                "StartTime"      : start_time,
                "EndTime"        : end_time,
                "Time"           : time_str,
                "TimeLabel"      : booking.get("TimeLabel", ""),
                "ResearchTopic"  : booking.get("ResearchTopic", ""),
                "ResearchDetail" : booking.get("ResearchDetail", ""),
                "Status"         : booking.get("Status", ""),
                "RescheduledOnce": rescheduled_once,  # ✅ เปลี่ยนจาก postpone_count
                "can_reschedule" : can_reschedule,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[BookingInfo] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# GET /reschedule/AvailableSlots
# ---------------------------------------------------------------------------
@router.get("/AvailableSlots")
def get_available_slots(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)

        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db = Connect_MongoDB()["BORC"]

        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {"AdvisorId": 1, "Date": 1, "Time": 1}
        )

        if not booking:
            return {"dates": {}}

        advisor_id = booking.get("AdvisorId", "")
        if not advisor_id:
            return {"dates": {}}

        return {"dates": get_reschedule_available_dates(db, advisor_id)}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[AvailableSlots] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# API เลื่อนคิวการจองของนักศึกษา
def _load_reschedulable_booking(db, user_id: str) -> tuple[dict, str, str]:
    """ดึงคิวที่นักศึกษาเลื่อนได้ พร้อมเวลาเดิม; raise HTTPException ถ้าเลื่อนไม่ได้"""
    booking = db["BookingOnline"].find_one(
        {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}}
    )
    if not booking:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

    if booking["Status"] != "Approved":
        raise HTTPException(
            status_code=400,
            detail="สามารถเลื่อนคิวได้เฉพาะเมื่อสถานะเป็น 'อนุมัติแล้ว' เท่านั้น"
        )

    if booking.get("RescheduledOnce", False):
        raise HTTPException(
            status_code=400,
            detail="คุณได้เลื่อนคิวไปแล้ว 1 ครั้ง ไม่สามารถเลื่อนได้อีก"
        )

    old_start, old_end = split_time_range(booking.get("Time", ""))
    if old_start and is_within_cutoff(booking.get("Date", ""), old_start):
        raise HTTPException(
            status_code=400,
            detail="ไม่สามารถเลื่อนคิวได้ เนื่องจากเหลือเวลาน้อยกว่า 1 ชั่วโมงก่อนเวลานัด"
        )

    return booking, old_start, old_end


def _insert_reschedule_history(db, user_id, booking, data, old_start, old_end, now) -> None:
    db["RescheduleHistory"].insert_one({
        "rescheduledById"  : user_id,
        "rescheduledByRole": "Student",
        "bookingId"        : str(booking["_id"]),
        "advisorName"      : booking.get("Advisor_Name", ""),
        "studentName"      : booking.get("StudentName", ""),
        "oldDate"          : booking.get("Date", ""),
        "oldStart"         : old_start,
        "oldEnd"           : old_end,
        "newDate"          : data.new_date,
        "newStart"         : data.new_start,
        "newEnd"           : data.new_end,
        "newLabel"         : data.new_label,
        "rescheduledReason": data.reason,
        "status"           : "Rescheduled",
        "createdAt"        : now,
        "updatedAt"        : now,
    })


@router.put("/RescheduleBooking")
def reschedule_booking(request: Request, background_tasks: BackgroundTasks, data: RescheduleForm = Depends()):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)

        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token เนื่องจากหมดอายุ")

        db = Connect_MongoDB()["BORC"]
        booking, old_start, old_end = _load_reschedulable_booking(db, user_id)

        if not data.reason.strip():
            raise HTTPException(status_code=400, detail="กรุณาระบุเหตุผลในการเลื่อนคิว")

        advisor_id   = booking.get("AdvisorId", "")
        advisor_name = booking.get("Advisor_Name", "")
        student_name = booking.get("StudentName", "")

        if advisor_id:
            ensure_slot_open_for_reschedule(db, advisor_id, data.new_date, data.new_start, data.new_end)

        now = datetime.now(timezone.utc)
        move_booking_to_slot(
            db, booking, advisor_id,
            data.new_date, data.new_start, data.new_end, old_start, old_end,
            extra_fields={"RescheduledOnce": True},
            now=now,
        )
        _insert_reschedule_history(db, user_id, booking, data, old_start, old_end, now)

        # แจ้งอาจารย์ว่านักศึกษาเลื่อนคิว (background — ไม่บล็อก event loop)
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyQueueAdivsor/RecheduleAdvisor",
            {
                "AdvisorId"  : advisor_id,  # userId ของ Advisor
                "StudentName": student_name,
                "Date"       : data.new_date,
                "Time"       : f"{data.new_start}-{data.new_end}",
                "Status"     : "Rescheduled"
            },
            CHATBOT_INTERNAL_HEADERS,
        )

        log_queue_management_history(
            db,
            advisor_id=advisor_id,
            advisor_name=advisor_name,
            student_id=user_id,
            student_name=student_name,
            status="Rescheduled",
            reason=data.reason,
            now=now,
        )

        return {"message": "เลื่อนคิวสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[RescheduleBooking] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

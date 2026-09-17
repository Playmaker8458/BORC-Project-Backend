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
from common.slot_service import is_within_cutoff, update_slot
from common.notify import notify_chatbot
from common.queue_history import log_queue_management_history


router = APIRouter()

load_dotenv(override=True)
chatbot_uri = os.getenv("ChatBot_URL")
# ส่ง shared-secret header ไปให้บริการ ChatBot ตรวจสอบว่า request มาจาก backend นี้จริง
CHATBOT_INTERNAL_HEADERS = {"X-Internal-Secret": os.getenv("INTERNAL_SERVICE_SECRET", "")}

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
async def get_booking_info(request: Request):
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
async def get_available_slots(request: Request):
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

        tz_utc7  = timezone(timedelta(hours=7))
        min_date = (datetime.now(timezone.utc).astimezone(tz_utc7) + timedelta(days=1)).strftime("%Y-%m-%d")

        docs = list(db["ManageTimeSlots"].find(
            {"advisorId": advisor_id},
            {"_id": 0, "dates": 1}
        ))

        if not docs:
            return {"dates": {}}

        merged_dates: dict = {}

        for doc in docs:
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list) or date < min_date:
                    continue

                available_slots = []
                for s in slots:
                    is_closed = s.get("isLocked", False) or s.get("is_closed", False)
                    booked    = s.get("booked", 0)
                    max_book  = s.get("max_booking", 1)
                    available = max(0, max_book - booked)

                    if is_closed or available <= 0:
                        continue

                    available_slots.append({
                        "start"      : s["start"],
                        "end"        : s["end"],
                        "label"      : s.get("label", ""),
                        "available"  : available,
                        "max_booking": max_book,
                    })

                if available_slots:
                    merged_dates[date] = available_slots

        return {"dates": merged_dates}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[AvailableSlots] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# API เลื่อนคิวการจองของนักศึกษา
@router.put("/RescheduleBooking")
async def reschedule_booking(request: Request, background_tasks: BackgroundTasks, data: RescheduleForm = Depends()):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)

        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token เนื่องจากหมดอายุ")

        db  = Connect_MongoDB()["BORC"]
        col   = db["BookingOnline"]
        booking = col.find_one(
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

        time_str   = booking.get("Time", "")
        time_parts = [t.strip() for t in time_str.split("-")] if "-" in time_str else []

        old_start  = time_parts[0] if len(time_parts) == 2 else ""
        old_end    = time_parts[1] if len(time_parts) == 2 else ""

        if old_start and is_within_cutoff(booking.get("Date", ""), old_start):
            raise HTTPException(
                status_code=400,
                detail="ไม่สามารถเลื่อนคิวได้ เนื่องจากเหลือเวลาน้อยกว่า 1 ชั่วโมงก่อนเวลานัด"
            )

        if not data.reason.strip():
            raise HTTPException(status_code=400, detail="กรุณาระบุเหตุผลในการเลื่อนคิว")

        advisor_id   = booking.get("AdvisorId", "")
        advisor_name = booking.get("Advisor_Name", "")

        # ตรวจ slot ใหม่ว่าว่างอยู่
        if advisor_id:
            slot_doc = db["ManageTimeSlots"].find_one(
                {
                    "advisorId"              : advisor_id,
                    f"dates.{data.new_date}" : {"$exists": True},
                },
                {"dates": 1}
            )
            if slot_doc:
                slots  = slot_doc.get("dates", {}).get(data.new_date, [])
                target = next(
                    (s for s in slots if s.get("start") == data.new_start and s.get("end") == data.new_end),
                    None
                )
                if not target:
                    raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาที่เลือก")
                if target.get("isLocked", False) or target.get("is_closed", False):
                    raise HTTPException(status_code=400, detail="ช่วงเวลานี้ปิดให้บริการแล้ว")
                if target.get("booked", 0) >= target.get("max_booking", 1):
                    raise HTTPException(status_code=400, detail="ช่วงเวลานี้เต็มแล้ว กรุณาเลือกช่วงเวลาอื่น")

        now = datetime.now(timezone.utc)
        booking_id = str(booking["_id"])

        # 1. อัปเดต booking
        col.update_one(
            {"_id": booking["_id"]},
            {"$set": {
                "Date"           : data.new_date,
                "Time"           : f"{data.new_start}-{data.new_end}",
                "Status"         : "Rescheduled",
                "RescheduledOnce": True,
                "UpdatedAt"      : now,
            }}
        )

        # 2. ✅ คืน slot เก่า
        if advisor_id and old_start and old_end:
            update_slot(db, advisor_id, booking.get("Date", ""), old_start, old_end, action="release")

        # 3. ✅ จอง slot ใหม่
        if advisor_id:
            update_slot(db, advisor_id, data.new_date, data.new_start, data.new_end, action="book")

        # 4. บันทึก history
        db["RescheduleHistory"].insert_one({
            "rescheduledById"  : user_id,
            "rescheduledByRole": "Student",
            "bookingId"        : booking_id,
            "advisorName"      : advisor_name,
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

        # ส่งการแจ้งเตือนไปให้ advisor นักศึกษาเลื่อนคิว (background — ไม่บล็อก event loop)
        advisor_id = booking.get("AdvisorId", "")
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyQueueAdivsor/RecheduleAdvisor",
            {
                "AdvisorId"  : advisor_id,  # userId ของ Advisor
                "StudentName": booking.get("StudentName", ""),
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
            student_name=booking.get("StudentName", ""),
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
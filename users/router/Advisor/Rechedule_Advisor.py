import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel
import requests as http_req  # ไม่ได้เรียกตรงนี้แล้ว (ใช้ common.notify แทน) แต่คงไว้เพราะ
                              # tests/test_security_fixes.py เข้าถึง module.http_req โดยตรง
from dotenv import load_dotenv
import os
from common.slot_service import is_within_advisor_cutoff_window, update_slot
from common.notify import notify_chatbot
from common.queue_history import log_queue_management_history


router = APIRouter()

load_dotenv(override=True)
chatbot_uri = os.getenv("ChatBot_URL")
# ส่ง shared-secret header ไปให้บริการ ChatBot ตรวจสอบว่า request มาจาก backend นี้จริง
CHATBOT_INTERNAL_HEADERS = {"X-Internal-Secret": os.getenv("INTERNAL_SERVICE_SECRET", "")}

ACTIVE_STATUSES = ["Pending", "Approved", "InProgress", "Rescheduled"]


class RescheduleBody(BaseModel):
    user_id   : str
    new_date  : str
    new_start : str
    new_end   : str
    new_label : str = ""
    reason    : str = ""


# ─── GET /advisor-reschedule/BookingInfo?user_id=<studentUserId> ──────────────
@router.get("/BookingInfo")
async def get_booking_info(user_id: str, request: Request):
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

        time_str   = booking.get("Time", "")
        time_parts = [t.strip() for t in time_str.split("-")] if "-" in time_str else []
        start_time = time_parts[0] if len(time_parts) == 2 else ""
        end_time   = time_parts[1] if len(time_parts) == 2 else ""

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
async def get_available_slots(user_id: str, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {"AdvisorId": 1}
        )

        if not booking:
            return {"dates": {}}

        if booking.get("AdvisorId", "") != advisor_id:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์จัดการคิวนี้")

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


# ─── PUT /advisor-reschedule/RescheduleBooking ────────────────────────────────
@router.put("/RescheduleBooking")
async def reschedule_booking(request: Request, body: RescheduleBody, background_tasks: BackgroundTasks):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        col     = db["BookingOnline"]
        booking = col.find_one(
            {"UserId": body.user_id, "Status": {"$in": ACTIVE_STATUSES}}
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

        # ✅ เช็คสิทธิ์อาจารย์จาก AdvisorRescheduledOnce — ไม่แตะ RescheduledOnce ของนักศึกษา
        if booking.get("AdvisorRescheduledOnce", False):
            raise HTTPException(
                status_code=400,
                detail="ไม่สามารถเลื่อนคิวได้อีก เนื่องจากอาจารย์เลื่อนคิวนี้ไปแล้ว 1 ครั้ง"
            )

        time_str   = booking.get("Time", "")
        time_parts = [t.strip() for t in time_str.split("-")] if "-" in time_str else []
        old_start  = time_parts[0] if len(time_parts) == 2 else ""
        old_end    = time_parts[1] if len(time_parts) == 2 else ""

        if old_start and is_within_advisor_cutoff_window(booking.get("Date", ""), old_start):
            raise HTTPException(
                status_code=400,
                detail="ไม่สามารถเลื่อนคิวได้ เนื่องจากอยู่ในช่วงเวลานัดหมาย (1 ชั่วโมงก่อน ถึง 1 ชั่วโมงหลังเวลาเริ่มนัด)"
            )

        # ตรวจ slot ใหม่ว่าว่างอยู่
        slot_doc = db["ManageTimeSlots"].find_one(
            {
                "advisorId" : advisor_id,
                f"dates.{body.new_date}" : {"$exists": True},
            },
            {"dates": 1}
        )
        if slot_doc:
            slots  = slot_doc.get("dates", {}).get(body.new_date, [])
            target = next(
                (s for s in slots if s.get("start") == body.new_start and s.get("end") == body.new_end),
                None
            )
            if not target:
                raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาที่เลือก")
            if target.get("isLocked", False) or target.get("is_closed", False):
                raise HTTPException(status_code=400, detail="ช่วงเวลานี้ปิดให้บริการแล้ว")
            if target.get("booked", 0) >= target.get("max_booking", 1):
                raise HTTPException(status_code=400, detail="ช่วงเวลานี้เต็มแล้ว กรุณาเลือกช่วงเวลาอื่น")

        now        = datetime.now(timezone.utc)
        booking_id = str(booking["_id"])

        # 1. อัปเดต booking
        # ✅ ใช้ AdvisorRescheduledOnce แทน RescheduledOnce — ไม่แตะสิทธิ์นักศึกษา
        col.update_one(
            {"_id": booking["_id"]},
            {"$set": {
                "Date"                  : body.new_date,
                "Time"                  : f"{body.new_start}-{body.new_end}",
                "Status"                : "Rescheduled",
                "AdvisorRescheduledOnce": True,   # ✅ แก้จาก RescheduledOnce → AdvisorRescheduledOnce
                "UpdatedAt"             : now,
            }}
        )

        # 2. ✅ คืน slot เก่า
        if old_start and old_end:
            update_slot(db, advisor_id, booking.get("Date", ""), old_start, old_end, action="release")

        # 3. ✅ จอง slot ใหม่
        update_slot(db, advisor_id, body.new_date, body.new_start, body.new_end, action="book")

        # 4. บันทึก RescheduleHistory
        db["RescheduleHistory"].insert_one({
            "rescheduledById"  : advisor_id,
            "rescheduledByRole": "Advisor",
            "bookingId"        : booking_id,
            "studentId"        : booking.get("UserId", ""),
            "advisorName"      : booking.get("Advisor_Name", ""),
            "studentName"      : booking.get("StudentName", ""),
            "oldDate"          : booking.get("Date", ""),
            "oldStart"         : old_start,
            "oldEnd"           : old_end,
            "newDate"          : body.new_date,
            "newStart"         : body.new_start,
            "newEnd"           : body.new_end,
            "newLabel"         : body.new_label,
            "rescheduledReason": body.reason,
            "status"           : "Rescheduled",
            "createdAt"        : now,
            "updatedAt"        : now,
        })
        
    # ส่งการแจ้งเตือนไปให้ user (background — ไม่บล็อก event loop)
        user_id_student = booking.get("UserId", "")
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyQueueStudent/RecheduleStudent",
            {
                "UserId"     : user_id_student,
                "StudentName": booking.get("StudentName", ""),
                "Date"       : body.new_date,
                "Time"       : f"{body.new_start}-{body.new_end}",
                "Status"     : "Rescheduled"
            },
            CHATBOT_INTERNAL_HEADERS,
        )


        log_queue_management_history(
            db,
            advisor_id=advisor_id,
            advisor_name=booking.get("Advisor_Name", ""),
            student_id=user_id_student,
            student_name=booking.get("StudentName", ""),
            status="Rescheduled",
            reason=body.reason,
            now=now,
        )

        return {"message": "เลื่อนคิวสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[RescheduleBooking] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")
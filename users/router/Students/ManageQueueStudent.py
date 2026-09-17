import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel
from ..Students.BookingOnline import auto_update_status, recalculate_slot_booked as sync_slot_booking
from dotenv import load_dotenv
import os
from common.notify import notify_chatbot
from common.queue_history import log_queue_management_history


router = APIRouter()

load_dotenv(override=True)
chatbot_uri = os.getenv("ChatBot_URL")
# ส่ง shared-secret header ไปให้บริการ ChatBot ตรวจสอบว่า request มาจาก backend นี้จริง
CHATBOT_INTERNAL_HEADERS = {"X-Internal-Secret": os.getenv("INTERNAL_SERVICE_SECRET", "")}

ACTIVE_STATUSES        = ["Pending", "Approved", "Rescheduled", "InProgress"]
CANCELLABLE_STATUSES   = ["Pending"]
SLOT_BLOCKING_STATUSES = ["Pending", "Approved"]  # ✅ เพิ่มกลับ

CUTOFF_HOURS_BEFORE = 1
CUTOFF_HOURS_AFTER  = 1


class CancelBookingRequest(BaseModel):
    cancelReason: str = ""


class RescheduleRequest(BaseModel):
    new_date: str
    new_time: str


# ─── Cutoff helper ────────────────────────────────────────────────────────────
def is_within_cutoff(date_str: str, start_time: str) -> bool:
    tz_utc7  = timezone(timedelta(hours=7))
    now_utc7 = datetime.now(timezone.utc).astimezone(tz_utc7)
    try:
        start_dt      = datetime.strptime(
            f"{date_str} {start_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=tz_utc7)
        cutoff_before = start_dt - timedelta(hours=CUTOFF_HOURS_BEFORE)
        cutoff_after  = start_dt + timedelta(hours=CUTOFF_HOURS_AFTER)
        return cutoff_before <= now_utc7 < cutoff_after
    except ValueError:
        return False


# ─── recalculate_slot_booked ──────────────────────────────────────────────────
def recalculate_slot_booked(db, advisor_name: str, date: str, start: str, end: str, new_status: str = None):
    try:
        col_slots    = db["ManageTimeSlots"]
        doc          = col_slots.find_one({"advisor_name": advisor_name}, {"dates": 1})
        if not doc:
            logger.warning(f"[WARN] recalculate_slot_booked: ไม่พบ doc ของ {advisor_name}")
            return

        slots        = doc.get("dates", {}).get(date, [])
        target_index = next(
            (i for i, s in enumerate(slots) if s["start"] == start and s["end"] == end),
            None
        )
        if target_index is None:
            logger.warning(f"[WARN] recalculate_slot_booked: ไม่พบ slot {start}-{end} วันที่ {date}")
            return

        if new_status == "Cancelled":
            col_slots.update_one(
                {"advisor_name": advisor_name},
                {
                    "$set": {
                        f"dates.{date}.{target_index}.booked"  : 0,
                        f"dates.{date}.{target_index}.isLocked": False,
                    },
                    "$unset": {
                        f"dates.{date}.{target_index}.is_closed": "",
                    }
                }
            )
    except Exception as e:
        logger.warning(f"[WARN] recalculate_slot_booked failed: {e}")


# ─── recalculate_slot_booked_live ─────────────────────────────────────────────
def recalculate_slot_booked_live(db, advisor_name: str, date: str, start: str, end: str):
    try:
        col_booking = db["BookingOnline"]
        col_slots   = db["ManageTimeSlots"]
        month       = date[:7]

        real_booked = col_booking.count_documents({
            "Advisor_Name": advisor_name,
            "Date"        : date,
            "Time"        : f"{start}-{end}",
            "Status"      : {"$in": SLOT_BLOCKING_STATUSES},
        })

        doc = col_slots.find_one(
            {"advisor_name": advisor_name, "month": month},
            {"dates": 1}
        )
        if not doc:
            return

        slots  = doc.get("dates", {}).get(date, [])
        target = next((s for s in slots if s["start"] == start and s["end"] == end), None)
        if not target:
            return

        max_booking     = target.get("max_booking", 1)
        manually_closed = target.get("manually_closed", False)
        new_is_closed   = (real_booked >= max_booking) or manually_closed if real_booked > 0 else False

        col_slots.update_one(
            {"advisor_name": advisor_name, "month": month},
            {"$set": {
                f"dates.{date}.$[slot].booked"         : real_booked,
                f"dates.{date}.$[slot].is_closed"      : new_is_closed,
                f"dates.{date}.$[slot].manually_closed": manually_closed if real_booked > 0 else False,
            }},
            array_filters=[{"slot.start": start, "slot.end": end}]
        )
    except Exception as e:
        logger.warning(f"[WARN] recalculate_slot_booked_live failed: {e}")





# ─── GET /MyBookingDetail ─────────────────────────────────────────────────────
@router.get('/MyBookingDetail')
async def get_my_booking_detail(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db = Connect_MongoDB()["BORC"]
        auto_update_status(db)

        active_bookings = list(
            db["BookingOnline"].find(
                {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}}
            )
        )

        if not active_bookings:
            return {"booking": None}

        booking = active_bookings[0]

        return {
            "booking": {
                "BookingId"      : str(booking["_id"]),
                "Advisor_Name"   : booking.get("Advisor_Name", ""),
                "Date"           : booking.get("Date", ""),
                "Time"           : booking.get("Time", ""),
                "TimeLabel"      : booking.get("TimeLabel", ""),
                "ResearchTopic"  : booking.get("ResearchTopic", ""),
                "ResearchDetail" : booking.get("ResearchDetail", ""),
                "Status"         : booking.get("Status", ""),
                "RescheduledOnce": booking.get("RescheduledOnce", False),
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[MyBookingDetail] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /CheckRescheduleEligibility ─────────────────────────────────────────
@router.get('/CheckRescheduleEligibility')
async def check_reschedule_eligibility(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {"_id": 1, "Status": 1, "RescheduledOnce": 1, "Date": 1, "Time": 1}
        )

        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

        time_parts    = booking.get("Time", "").split("-")
        start         = time_parts[0].strip() if len(time_parts) == 2 else ""
        within_cutoff = is_within_cutoff(booking.get("Date", ""), start) if start else False

        can_reschedule = (
            booking["Status"] == "Approved"           and
            not booking.get("RescheduledOnce", False) and
            not within_cutoff
        )

        return {"can_reschedule": can_reschedule}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[CheckRescheduleEligibility] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /CheckRescheduleHistory ─────────────────────────────────────────────
@router.get('/CheckRescheduleHistory')
async def check_reschedule_history(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}},
            {"_id": 1}
        )
        if not booking:
            return {"has_rescheduled": False}

        booking_id = str(booking["_id"])
        count      = db["RescheduleHistory"].count_documents({
            "rescheduledById"  : user_id,
            "rescheduledByRole": "Student",
            "bookingId"        : booking_id,
        })

        return {"has_rescheduled": count > 0}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[CheckRescheduleHistory] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── PUT /StudentReschedule ───────────────────────────────────────────────────
@router.put('/StudentReschedule')
async def student_reschedule(request: Request, body: RescheduleRequest):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db      = Connect_MongoDB()["BORC"]
        col     = db["BookingOnline"]
        booking = col.find_one({"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}})

        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

        if booking["Status"] != "Approved":
            raise HTTPException(status_code=400, detail="เลื่อนได้เฉพาะสถานะ Approved เท่านั้น")

        if booking.get("RescheduledOnce", False):
            raise HTTPException(
                status_code=400,
                detail="ไม่สามารถเลื่อนคิวได้อีก เนื่องจากเลื่อนคิวไปแล้ว 1 ครั้ง"
            )

        time_parts = booking.get("Time", "").split("-")
        start_time = time_parts[0].strip() if len(time_parts) == 2 else ""
        if start_time and is_within_cutoff(booking["Date"], start_time):
            raise HTTPException(
                status_code=400,
                detail="ไม่สามารถเลื่อนคิวได้ เนื่องจากอยู่ในช่วงเวลานัดหมาย"
            )

        advisor_doc = db["ManageTimeSlots"].find_one({"advisor_name": booking["Advisor_Name"]})
        if advisor_doc:
            slots      = advisor_doc.get("dates", {}).get(body.new_date, [])
            time_parts = body.new_time.split("-")
            if len(time_parts) == 2:
                start, end = time_parts[0].strip(), time_parts[1].strip()
                target     = next((s for s in slots if s["start"] == start and s["end"] == end), None)
                if not target:
                    raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาที่เลือก")
                if target.get("is_closed", False):
                    raise HTTPException(status_code=400, detail="ช่วงเวลานี้ปิดให้บริการแล้ว")
                if target.get("booked", 0) >= target["max_booking"]:
                    raise HTTPException(status_code=400, detail="ช่วงเวลานี้เต็มแล้ว")

        now        = datetime.now(timezone.utc)
        booking_id = str(booking["_id"])

        col.update_one(
            {"_id": booking["_id"]},
            {"$set": {
                "Date"           : body.new_date,
                "Time"           : body.new_time,
                "Status"         : "Rescheduled",
                "RescheduledOnce": True,
                "UpdatedAt"      : now,
            }}
        )

        db["RescheduleHistory"].insert_one({
            "rescheduledById"  : user_id,
            "rescheduledByRole": "Student",
            "bookingId"        : booking_id,
            "advisorName"      : booking.get("Advisor_Name", ""),
            "studentName"      : booking.get("StudentName", ""),
            "oldDate"          : booking.get("Date", ""),
            "oldTime"          : booking.get("Time", ""),
            "newDate"          : body.new_date,
            "newTime"          : body.new_time,
            "rescheduledReason": "",
            "status"           : "Rescheduled",
            "createdAt"        : now,
            "updatedAt"        : now,
        })

        return {"message": "เลื่อนคิวสำเร็จ รอการยืนยันจากอาจารย์"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[StudentReschedule] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── DELETE /CancelBooking ────────────────────────────────────────────────────
@router.delete('/CancelBooking')
async def cancel_booking(request: Request, body: CancelBookingRequest):
    logger.info(f" cancelReason: {body.cancelReason}")
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db          = Connect_MongoDB()["BORC"]
        col_booking = db["BookingOnline"]
        booking     = col_booking.find_one(
            {"UserId": user_id, "Status": {"$in": ACTIVE_STATUSES}}
        )

        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

        if booking["Status"] not in CANCELLABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"ไม่สามารถยกเลิกได้ เนื่องจากสถานะปัจจุบันคือ '{booking['Status']}'"
            )

        advisor_name = booking.get("Advisor_Name", "")
        date         = booking.get("Date", "")
        time_str     = booking.get("Time", "")
        time_parts   = time_str.split("-") if time_str else []
        start        = time_parts[0].strip() if len(time_parts) == 2 else ""
        end          = time_parts[1].strip() if len(time_parts) == 2 else ""

        if start and is_within_cutoff(date, start):
            raise HTTPException(
                status_code=400,
                detail="ไม่สามารถยกเลิกได้ เนื่องจากอยู่ในช่วงเวลานัดหมาย"
            )

        now = datetime.now(timezone.utc)

        col_booking.update_one(
            {"_id": booking["_id"]},
            {"$set": {
                "Status": "Cancelled", 
                "CancelReason": body.cancelReason.strip(),
                "UpdatedAt": now
            }}
        )

        if booking.get("AdvisorId") and date and start and end:
            sync_slot_booking(db, booking["AdvisorId"], date, start, end)

        db["CancelBookingHistory"].insert_one({
            "cancelledById"  : user_id,
            "cancelledByRole": "Student",
            "advisorName"    : advisor_name,
            "studentName"    : booking.get("StudentName", ""),
            "status"         : "Cancelled",
            "cancelReason"   : body.cancelReason.strip(),
            "createdAt"      : now,
            "updatedAt"      : now,
        })

        log_queue_management_history(
            db,
            advisor_id=booking.get("AdvisorId", ""),
            advisor_name=advisor_name,
            student_id=user_id,
            student_name=booking.get("StudentName", ""),
            status="Cancelled",
            reason=body.cancelReason.strip(),
            now=now,
        )

        # ดึง AdvisorId จาก booking เพื่อใช้ส่งการแจ้งเตือน
        advisor_id = booking.get("AdvisorId", "")
        logger.info(f" AdvisorId: {advisor_id}")
        # แจ้งเตือนยกเลิกการจองของนักศึกษาส่งให้กับ อาจารย์
        notify_chatbot(f"{chatbot_uri}/NotifyCancelled/CancelBookingAdvisor", {
            "AdvisorId"   : advisor_id,
            "StudentName" : booking.get("StudentName", ""),
            "Date"        : date,
            "Time"        : time_str
        }, CHATBOT_INTERNAL_HEADERS)

        return {"message": "ยกเลิกการจองสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[CancelBooking] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /MyCancelCount ───────────────────────────────────────────────────────
@router.get('/MyCancelCount')
async def get_my_cancel_count(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db          = Connect_MongoDB()["BORC"]
        self_cancel = db["CancelBookingHistory"].count_documents({
            "cancelledById"  : user_id,
            "cancelledByRole": "Student",
        })

        return {"self_cancel": self_cancel}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[MyCancelCount] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone
from pydantic import BaseModel
from ..Students.BookingOnline import auto_update_status, recalculate_slot_booked as sync_slot_booking
from dotenv import load_dotenv
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.slot_service import is_within_advisor_cutoff_window
from common.queue_history import log_queue_management_history


router = APIRouter()

load_dotenv(override=True)
chatbot_uri = CHATBOT_URL

ACTIVE_STATUSES        = ["Pending", "Approved", "Rescheduled", "InProgress"]
CANCELLABLE_STATUSES   = ["Pending", "Approved", "Rescheduled"]
SLOT_BLOCKING_STATUSES = ["Pending", "Approved"]  # ✅ เพิ่มกลับ

CUTOFF_HOURS_BEFORE = 1
CUTOFF_HOURS_AFTER  = 1


class CancelBookingRequest(BaseModel):
    cancelReason: str = ""


# ─── Cutoff helper ────────────────────────────────────────────────────────────
# นักศึกษายกเลิก/เลื่อนคิวถูกล็อกทั้งก่อนและหลังเวลานัด 1 ชม. (พฤติกรรมเดิมของไฟล์นี้)
# จึงใช้ฟังก์ชันหน้าต่างเวลาตัวเดียวกับฝั่งอาจารย์ใน common/slot_service.py
is_within_cutoff = is_within_advisor_cutoff_window





# ─── GET /MyBookingDetail ─────────────────────────────────────────────────────
@router.get('/MyBookingDetail')
def get_my_booking_detail(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db = Connect_MongoDB()["BORC"]

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
def check_reschedule_eligibility(request: Request):
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
def check_reschedule_history(request: Request):
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


# ─── DELETE /CancelBooking ────────────────────────────────────────────────────
@router.delete('/CancelBooking')
def cancel_booking(request: Request, body: CancelBookingRequest, background_tasks: BackgroundTasks):
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
        # แจ้งเตือนยกเลิกการจองของนักศึกษาส่งให้กับ อาจารย์ (background — ไม่บล็อก event loop)
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyCancelled/CancelBookingAdvisor",
            {
                "AdvisorId"   : advisor_id,
                "StudentName" : booking.get("StudentName", ""),
                "Date"        : date,
                "Time"        : time_str
            },
            CHATBOT_INTERNAL_HEADERS,
        )

        return {"message": "ยกเลิกการจองสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[CancelBooking] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /MyCancelCount ───────────────────────────────────────────────────────
@router.get('/MyCancelCount')
def get_my_cancel_count(request: Request):
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

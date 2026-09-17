import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone,timedelta
from pydantic import BaseModel
from dotenv import load_dotenv
import os
from ..Students.BookingOnline import auto_update_status, recalculate_slot_booked as sync_slot_booking
from common.slot_service import is_within_advisor_cutoff_window, can_cancel_approved
from common.notify import notify_chatbot
from common.queue_history import log_queue_management_history

router = APIRouter()

load_dotenv(override=True)
chatbot_uri = os.getenv("ChatBot_URL")
# ส่ง shared-secret header ไปให้บริการ ChatBot ตรวจสอบว่า request มาจาก backend นี้จริง
CHATBOT_INTERNAL_HEADERS = {"X-Internal-Secret": os.getenv("INTERNAL_SERVICE_SECRET", "")}

# ✅ เพิ่ม InProgress เข้า ACTIVE_STATUSES
ACTIVE_STATUSES      = ["Pending", "Approved", "Rescheduled", "InProgress"]
CANCELLABLE_STATUSES = ["Pending", "Approved"]
CONFIRMABLE_STATUSES = ["Pending", "Rescheduled"]


# ─── PUT /ConfirmQueue ────────────────────────────────────────────────────────
class ConfirmBody(BaseModel):
    user_id: str


# ─── DELETE /AdvisorCancelQueue ───────────────────────────────────────────────
class CancelBody(BaseModel):
    user_id: str
    reason : str = ""


class CompleteBody(BaseModel):
    user_id: str



# ─── GET /AdvisorQueues ───────────────────────────────────────────────────────
@router.get("/AdvisorQueues")
async def get_advisor_queues(request: Request):
    try:
        payload      = verify_user_token(request)
        advisor_id   = get_user_id(payload)
        advisor_name = f"{payload['Prefix']}{payload['Firstname']} {payload['Lastname']}"

        db = Connect_MongoDB()["BORC"]
        auto_update_status(db)

        bookings = list(
            db["BookingOnline"].find(
                {"AdvisorId": advisor_id, "Status": {"$in": ACTIVE_STATUSES}}
            ).sort("Date", 1)
        )

        result = []
        for b in bookings:
            booking_id           = str(b["_id"])
            reschedule_count     = db["RescheduleHistory"].count_documents({
                "rescheduledById"  : advisor_id,
                "rescheduledByRole": "Advisor",
                "bookingId"        : booking_id,
            })
            b["has_rescheduled"] = reschedule_count > 0

            time_parts = b.get("Time", "").split("-")
            start_time = time_parts[0].strip() if len(time_parts) == 2 else ""
            b["can_cancel_approved"] = (
                can_cancel_approved(b.get("Date", ""), start_time)
                if b["Status"] == "Approved" and start_time
                else None
            )

            b["_id"] = booking_id
            result.append(b)

        return {"queues": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/ConfirmQueue")
async def confirm_queue(request: Request, body: ConfirmBody):
    try:
        payload = verify_user_token(request)
        advisor_id = get_user_id(payload)

        db      = Connect_MongoDB()["BORC"]
        col     = db["BookingOnline"]
        booking = col.find_one({
            "UserId": body.user_id,
            "AdvisorId": advisor_id,
            "Status": {"$in": ACTIVE_STATUSES},
        })

        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

        if booking["Status"] not in CONFIRMABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"อนุมัติได้เฉพาะสถานะ Pending หรือ Rescheduled เท่านั้น (ปัจจุบัน: {booking['Status']})"
            )

        now = datetime.now(timezone.utc)

        col.update_one(
            {"_id": booking["_id"]},
            {"$set": {
                "Status"                : "Approved",
                "AdvisorRescheduledOnce": False,
                "UpdatedAt"             : now,
            }}
        )


        # ✅ แจ้งเตือนนักศึกษา สีเขียว
        notify_chatbot(f"{chatbot_uri}/NotifyQueueStudent/NotifyStudent", {
            "userId"      : booking.get("UserId", ""),
            "StudentName" : booking.get("StudentName", ""),
            "AdvisorName" : booking.get("Advisor_Name", ""),
            "Date"        : booking.get("Date", ""),
            "Time"        : booking.get("Time", ""),
            "Status"      : "Approved"
        }, CHATBOT_INTERNAL_HEADERS)
            
        db["ApprovedHistory"].insert_one({
            "UserId"      : booking["UserId"],
            "StudentName" : booking.get("StudentName", ""),
            "AdvisorId"   : booking.get("AdvisorId", ""),
            "AdvisorName" : booking.get("Advisor_Name", ""),
            "Date"        : booking.get("Date", ""),
            "Time"        : booking.get("Time", ""),
            "Status"      : "Approved",
            "ApprovedFrom": booking.get("Status", ""),
            "CreatedAt"   : now,
            "UpdatedAt"   : now,
        })


        log_queue_management_history(
            db,
            advisor_id=booking.get("AdvisorId", ""),
            advisor_name=booking.get("Advisor_Name", ""),
            student_id=booking.get("UserId", ""),
            student_name=booking.get("StudentName", ""),
            status="Approved",
            reason=None,
            now=now,
        )

        return {"message": "อนุมัติคิวสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/AdvisorCancelQueue")
async def advisor_cancel_queue(request: Request, body: CancelBody):
    try:
        payload = verify_user_token(request)
        advisor_id = get_user_id(payload)

        db          = Connect_MongoDB()["BORC"]
        col_booking = db["BookingOnline"]
        booking     = col_booking.find_one({
            "UserId": body.user_id,
            "AdvisorId": advisor_id,
            "Status": {"$in": ACTIVE_STATUSES},
        })

        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจอง")

        if booking["Status"] not in CANCELLABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"ไม่สามารถยกเลิกได้ เนื่องจากสถานะปัจจุบันคือ '{booking['Status']}'"
            )

        advisor_name = booking.get("Advisor_Name", "")
        date_str     = booking.get("Date", "")
        time_str     = booking.get("Time", "")
        time_parts   = time_str.split("-") if time_str else []
        start        = time_parts[0].strip() if len(time_parts) == 2 else ""
        end          = time_parts[1].strip() if len(time_parts) == 2 else ""

        if booking["Status"] == "Pending":
            if start and is_within_advisor_cutoff_window(date_str, start):
                raise HTTPException(
                    status_code=400,
                    detail="ไม่สามารถยกเลิกได้ เนื่องจากอยู่ในช่วงเวลานัดหมาย"
                )
        elif booking["Status"] == "Approved":
            if not start or not can_cancel_approved(date_str, start):
                raise HTTPException(
                    status_code=400,
                    detail="ไม่สามารถยกเลิกได้ เนื่องจากถึงช่วงเวลาล็อคแล้ว (ก่อนเวลานัด 1 ชั่วโมง)"
                )

        now = datetime.now(timezone.utc)

        col_booking.update_one(
            {"_id": booking["_id"]},
            {"$set": {"Status": "Cancelled", "UpdatedAt": now}}
        )

        db["CancelBookingHistory"].insert_one({
            "cancelledById"  : booking.get("UserId", ""),
            "cancelledByRole": "Advisor",
            "advisorName"    : advisor_name,
            "studentName"    : booking.get("StudentName", ""),
            "cancelledDate"  : date_str,
            "cancelReason"   : body.reason,
            "status"         : "Cancelled",
            "createdAt"      : now,
            "updatedAt"      : now,
        })


        log_queue_management_history(
            db,
            advisor_id=booking.get("AdvisorId", ""),
            advisor_name=advisor_name,
            student_id=booking.get("UserId", ""),
            student_name=booking.get("StudentName", ""),
            status="Cancelled",
            reason=body.reason,
            now=now,
        )

        if booking.get("AdvisorId") and date_str and start and end:
            sync_slot_booking(db, booking["AdvisorId"], date_str, start, end)

              # ✅ แจ้งเตือนนักศึกษา สีแดง
        notify_chatbot(f"{chatbot_uri}/NotifyQueueStudent/NotifyStudent", {
            "userId"      : booking.get("UserId", ""),
            "StudentName" : booking.get("StudentName", ""),
            "AdvisorName" : advisor_name,
            "Date"        : date_str,
            "Time"        : time_str,
            "Status"      : "Cancelled"
        }, CHATBOT_INTERNAL_HEADERS)

        return {"message": "ยกเลิกคิวสำเร็จ นักศึกษาสามารถจองคิวใหม่ได้"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── PATCH /CompleteQueue ───────────────────────────────────────────────────
@router.patch("/CompleteQueue")
async def complete_queue(request: Request, body: CompleteBody):
    """ให้อาจารย์ปิดการให้คำปรึกษาด้วยตนเอง และปลด slot ทันที."""
    try:
        payload = verify_user_token(request)
        advisor_id = get_user_id(payload)

        db = Connect_MongoDB()["BORC"]
        col_booking = db["BookingOnline"]
        booking = col_booking.find_one({
            "UserId": body.user_id,
            "AdvisorId": advisor_id,
            "Status": {"$in": ["Approved", "InProgress"]},
        })
        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบคิวที่สามารถปิดการให้คำปรึกษาได้")

        now = datetime.now(timezone.utc)
        col_booking.update_one(
            {"_id": booking["_id"]},
            {"$set": {"Status": "Completed", "CompletedAt": now, "UpdatedAt": now}},
        )
        log_queue_management_history(
            db,
            advisor_id=advisor_id,
            advisor_name=booking.get("Advisor_Name", ""),
            student_id=booking.get("UserId", ""),
            student_name=booking.get("StudentName", ""),
            status="Completed",
            reason=None,
            now=now,
        )

        time_parts = booking.get("Time", "").split("-")
        if len(time_parts) == 2:
            sync_slot_booking(
                db,
                booking.get("AdvisorId", ""),
                booking.get("Date", ""),
                time_parts[0].strip(),
                time_parts[1].strip(),
            )

        return {"message": "ปิดการให้คำปรึกษาเรียบร้อย นักศึกษาสามารถจองคิวใหม่ได้"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── POST /SyncAdvisorSlots ───────────────────────────────────────────────────
@router.post("/SyncAdvisorSlots")
async def sync_advisor_slots(request: Request):
    try:
        payload      = verify_user_token(request)
        advisor_id   = get_user_id(payload)
        advisor_name = f"{payload['Prefix']}{payload['Firstname']} {payload['Lastname']}"

        db            = Connect_MongoDB()["BORC"]
        col_slots     = db["ManageTimeSlots"]
        docs          = list(col_slots.find({"advisorId": advisor_id}, {"advisorId": 1, "dates": 1}))
        updated_count = 0

        for doc in docs:
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list):
                    continue
                for s in slots:
                    start = s.get("start", "")
                    end   = s.get("end", "")
                    if start and end:
                        sync_slot_booking(db, advisor_id, date, start, end)
                        updated_count += 1

        return {"message": f"Sync เสร็จสิ้น อัปเดต {updated_count} slots ของ {advisor_name}", "updated_slots": updated_count}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

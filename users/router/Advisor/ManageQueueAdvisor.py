import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel
from dotenv import load_dotenv
from common.slot_service import (
    can_cancel_approved,
    has_started,
    is_within_advisor_cutoff_window,
    mark_booking_cancelled,
    recalculate_slot_booked,
    split_time_range,
    sync_slot_for_booking,
)
from common.attachments import content_disposition, iter_file, open_attachment
from common.booking_status import (
    ACTIVE_STATUSES,
    CANCELLABLE_STATUSES,
    COMPLETABLE_STATUSES,
    CONFIRMABLE_STATUSES,
)
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.parallel import run_parallel
from common.queue_history import log_queue_management_history

router = APIRouter()

load_dotenv(override=True)
chatbot_uri = CHATBOT_URL


# ─── PUT /ConfirmQueue ────────────────────────────────────────────────────────
class ConfirmBody(BaseModel):
    user_id: str


# ─── DELETE /AdvisorCancelQueue ───────────────────────────────────────────────
class CancelBody(BaseModel):
    user_id: str
    reason : str = ""


class CompleteBody(BaseModel):
    user_id: str



# ฟิลด์ของ booking ที่หน้าจัดการคิวของอาจารย์ใช้จริง (ไม่ส่งรายละเอียดงานวิจัย/ไฟล์แนบ ฯลฯ ให้เปลืองข้อมูล)
ADVISOR_QUEUE_FIELDS = {
    "UserId": 1, "AdvisorId": 1, "StudentName": 1, "ResearchTopic": 1,
    "Date": 1, "Time": 1, "Status": 1, "RescheduledOnce": 1,
    "FileName": 1, "FileId": 1,
}


# ─── GET /AdvisorQueues ───────────────────────────────────────────────────────
@router.get("/AdvisorQueues")
def get_advisor_queues(request: Request):
    try:
        payload      = verify_user_token(request)
        advisor_id   = get_user_id(payload)
        advisor_name = f"{payload['Prefix']}{payload['Firstname']} {payload['Lastname']}"

        db = Connect_MongoDB()["BORC"]

        # ดึงคิวกับประวัติเลื่อนคิวของอาจารย์พร้อมกัน (รอ DB รอบเดียว) แล้วจับคู่กันในหน่วยความจำ
        # (เดิมเป็น count_documents ต่อคิว = N+1 ข้ามเครือข่ายไป Atlas)
        bookings, rescheduled_docs = run_parallel(
            lambda: list(
                db["BookingOnline"].find(
                    {"AdvisorId": advisor_id, "Status": {"$in": ACTIVE_STATUSES}},
                    ADVISOR_QUEUE_FIELDS,
                ).sort("Date", 1)
            ),
            lambda: list(
                db["RescheduleHistory"].find(
                    {"rescheduledById": advisor_id, "rescheduledByRole": "Advisor"},
                    {"bookingId": 1, "_id": 0},
                )
            ),
        )
        rescheduled_ids = {h["bookingId"] for h in rescheduled_docs}

        result = []
        for b in bookings:
            booking_id           = str(b["_id"])
            b["has_rescheduled"] = booking_id in rescheduled_ids

            start_time, _ = split_time_range(b.get("Time", ""))
            b["can_cancel_approved"] = (
                can_cancel_approved(b.get("Date", ""), start_time)
                if b["Status"] == "Approved" and start_time
                else None
            )

            # ส่งแค่ว่ามีไฟล์แนบหรือไม่กับชื่อไฟล์ — id ภายในของ GridFS ไม่ให้หลุดออกไปหน้าเว็บ
            b["has_attachment"] = bool(b.pop("FileId", None))

            b["_id"] = booking_id
            result.append(b)

        return {"queues": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/Attachment/{booking_id}")
def download_attachment(booking_id: str, request: Request):
    """ดาวน์โหลดไฟล์แนบของคิว — เฉพาะอาจารย์เจ้าของคิว (สตรีมจาก GridFS ผ่าน backend ไม่มี URL สาธารณะ)"""
    try:
        payload = verify_user_token(request)
        advisor_id = get_user_id(payload)

        try:
            booking_oid = ObjectId(booking_id)
        except (InvalidId, TypeError):
            raise HTTPException(status_code=404, detail="ไม่พบไฟล์แนบ")

        db = Connect_MongoDB()["BORC"]
        booking = db["BookingOnline"].find_one(
            {"_id": booking_oid, "AdvisorId": advisor_id},
            {"FileId": 1, "FileName": 1, "FileContentType": 1},
        )
        if not booking or not booking.get("FileId"):
            raise HTTPException(status_code=404, detail="ไม่พบไฟล์แนบ")

        grid_out = open_attachment(db, booking["FileId"])
        if grid_out is None:
            raise HTTPException(status_code=404, detail="ไม่พบไฟล์แนบ")

        return StreamingResponse(
            iter_file(grid_out),
            media_type=booking.get("FileContentType") or "application/octet-stream",
            headers={
                "Content-Disposition": content_disposition(booking.get("FileName") or "attachment"),
                "Content-Length": str(grid_out.length),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/ConfirmQueue")
def confirm_queue(request: Request, body: ConfirmBody, background_tasks: BackgroundTasks):
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

        # อนุมัติเฉพาะเมื่อยัง Pending/Rescheduled จริง (นักศึกษาอาจยกเลิกพร้อมกัน — เดิมเขียนทับจนคิวที่
        # ยกเลิกแล้วกลายเป็น Approved ทั้งที่ slot ถูกคืนไปแล้ว) ต้องสำเร็จก่อนจึงแจ้งเตือน/เขียนประวัติ
        confirmed = col.update_one(
            {"_id": booking["_id"], "Status": {"$in": CONFIRMABLE_STATUSES}},
            {"$set": {
                "Status"                : "Approved",
                "AdvisorRescheduledOnce": False,
                "UpdatedAt"             : now,
            }}
        )
        if confirmed.matched_count == 0:
            raise HTTPException(status_code=409, detail="สถานะคิวเปลี่ยนไปแล้ว กรุณารีเฟรชหน้าแล้วลองใหม่อีกครั้ง")

        # ✅ แจ้งเตือนนักศึกษา สีเขียว (background — ไม่บล็อก event loop)
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyQueueStudent/NotifyStudent",
            {
                "userId"      : booking.get("UserId", ""),
                "StudentName" : booking.get("StudentName", ""),
                "AdvisorName" : booking.get("Advisor_Name", ""),
                "Date"        : booking.get("Date", ""),
                "Time"        : booking.get("Time", ""),
                "Status"      : "Approved"
            },
            CHATBOT_INTERNAL_HEADERS,
        )

        # 2 คำสั่งเขียนประวัตินี้ไม่พึ่งผลของกัน จึงรันพร้อมกัน
        run_parallel(
            lambda: db["ApprovedHistory"].insert_one({
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
            }),
            lambda: log_queue_management_history(
                db,
                advisor_id=booking.get("AdvisorId", ""),
                advisor_name=booking.get("Advisor_Name", ""),
                student_id=booking.get("UserId", ""),
                student_name=booking.get("StudentName", ""),
                status="Approved",
                reason=None,
                now=now,
            ),
        )

        return {"message": "อนุมัติคิวสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/AdvisorCancelQueue")
def advisor_cancel_queue(request: Request, body: CancelBody, background_tasks: BackgroundTasks):
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
        start, _     = split_time_range(time_str)

        if booking["Status"] in ("Pending", "Rescheduled"):
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

        # เปลี่ยนสถานะก่อนและต้องสำเร็จ (ถ้าสถานะเปลี่ยนไปแล้วห้ามเขียนประวัติ/คืน slot) ส่วน sync slot
        # กับ history 2 รายการไม่พึ่งกัน จึงรันพร้อมกัน
        if not mark_booking_cancelled(db, booking, now):
            raise HTTPException(status_code=409, detail="สถานะคิวเปลี่ยนไปแล้ว กรุณารีเฟรชหน้าแล้วลองใหม่อีกครั้ง")

        # คิวถูกยกเลิกแล้วใน DB — งานต่อจากนี้ (sync slot / เขียนประวัติ) ห้ามทำให้ request ล้ม
        # เดิมถ้าตัวใดตัวหนึ่ง raise จะตอบ 500 ทั้งที่ยกเลิกสำเร็จ และ background task แจ้งเตือน LINE
        # ไม่ถูกรันเลย (FastAPI ไม่รัน background tasks เมื่อ response เป็น error)
        try:
            run_parallel(
                lambda: sync_slot_for_booking(db, booking),
                lambda: db["CancelBookingHistory"].insert_one({
                    "cancelledById"  : booking.get("UserId", ""),
                    "cancelledByRole": "Advisor",
                    "advisorId"      : advisor_id,
                    "advisorName"    : advisor_name,
                    "studentName"    : booking.get("StudentName", ""),
                    "cancelledDate"  : date_str,
                    "cancelReason"   : body.reason,
                    "status"         : "Cancelled",
                    "createdAt"      : now,
                    "updatedAt"      : now,
                }),
                lambda: log_queue_management_history(
                    db,
                    advisor_id=booking.get("AdvisorId", ""),
                    advisor_name=advisor_name,
                    student_id=booking.get("UserId", ""),
                    student_name=booking.get("StudentName", ""),
                    status="Cancelled",
                    reason=body.reason,
                    now=now,
                ),
            )
        except Exception:
            logger.exception("[Cancel] ยกเลิกคิว %s แล้ว แต่ sync slot/เขียนประวัติไม่สำเร็จ", booking["_id"])

        # ✅ แจ้งเตือนนักศึกษา สีแดง (background — ไม่บล็อก event loop)
        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/NotifyQueueStudent/NotifyStudent",
            {
                "userId"      : booking.get("UserId", ""),
                "StudentName" : booking.get("StudentName", ""),
                "AdvisorName" : advisor_name,
                "Date"        : date_str,
                "Time"        : time_str,
                "Status"      : "Cancelled"
            },
            CHATBOT_INTERNAL_HEADERS,
        )

        return {"message": "ยกเลิกคิวสำเร็จ นักศึกษาสามารถจองคิวใหม่ได้"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── PATCH /CompleteQueue ───────────────────────────────────────────────────
@router.patch("/CompleteQueue")
def complete_queue(request: Request, body: CompleteBody):
    """ให้อาจารย์ปิดการให้คำปรึกษาด้วยตนเอง และปลด slot ทันที."""
    try:
        payload = verify_user_token(request)
        advisor_id = get_user_id(payload)

        db = Connect_MongoDB()["BORC"]
        col_booking = db["BookingOnline"]
        booking = col_booking.find_one({
            "UserId": body.user_id,
            "AdvisorId": advisor_id,
            "Status": {"$in": COMPLETABLE_STATUSES},
        })
        if not booking:
            raise HTTPException(status_code=404, detail="ไม่พบคิวที่สามารถปิดการให้คำปรึกษาได้")

        # ปิดคิวได้เมื่อการให้คำปรึกษาเริ่มแล้วเท่านั้น: InProgress หรือ Approved ที่ถึงเวลานัดแล้ว
        # (worker เปลี่ยน Approved → InProgress ทุก 30 วินาที จึงอาจยังเป็น Approved อยู่ชั่วครู่)
        # เดิมปิดคิวที่ Approved ล่วงหน้าได้ ทำให้ slot ถูกปล่อยและมีคนจองซ้อนก่อนถึงเวลา
        if booking["Status"] == "Approved":
            start, _ = split_time_range(booking.get("Time", ""))
            if not start or not has_started(booking.get("Date", ""), start):
                raise HTTPException(status_code=400, detail="ยังไม่ถึงเวลานัด ไม่สามารถปิดการให้คำปรึกษาได้")

        now = datetime.now(timezone.utc)
        completed = col_booking.update_one(
            {"_id": booking["_id"], "Status": {"$in": COMPLETABLE_STATUSES}},
            {"$set": {"Status": "Completed", "CompletedAt": now, "UpdatedAt": now}},
        )
        if completed.matched_count == 0:
            raise HTTPException(status_code=409, detail="สถานะคิวเปลี่ยนไปแล้ว กรุณารีเฟรชหน้าแล้วลองใหม่อีกครั้ง")
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
            recalculate_slot_booked(
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
def sync_advisor_slots(request: Request):
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
                        recalculate_slot_booked(db, advisor_id, date, start, end)
                        updated_count += 1

        return {"message": f"Sync เสร็จสิ้น อัปเดต {updated_count} slots ของ {advisor_name}", "updated_slots": updated_count}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

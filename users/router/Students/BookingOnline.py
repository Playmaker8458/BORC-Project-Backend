import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form, Depends, BackgroundTasks
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone, timedelta
from pymongo import ASCENDING, DESCENDING
import requests as req
from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError
from common.slot_service import get_now_utc7, is_within_cutoff, recalculate_slot_booked, CUTOFF_HOURS
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot

router = APIRouter()

load_dotenv(override=True)
chatbot_uri = CHATBOT_URL

# ─── Constants ────────────────────────────────────────────────────────────────
ALLOWED_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
]
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Status groups (SLOT_BLOCKING_STATUSES/CUTOFF_HOURS มาจาก common.slot_service
# เพื่อให้ทุกไฟล์ที่ sync slot ใช้ค่าเดียวกัน)
ACTIVE_STATUSES = ["Pending", "Approved", "InProgress", "Rescheduled"]
# นักศึกษามีนัดที่ยังใช้งานอยู่ได้เพียงหนึ่งรายการ ห้ามเขียนทับนัดที่
# Approved/Rescheduled เพราะนัดเดิมจะหายไปโดยไม่ถูกยกเลิกหรือบันทึกประวัติ
BLOCK_BOOKING_STATUSES  = ACTIVE_STATUSES
UPDATE_BOOKING_STATUSES = ["Approved", "Rescheduled"]

# Auto-update config
# Background worker ตรวจทุก 30 วินาที จึงเปลี่ยนสถานะใกล้เวลาจริงได้
AUTO_UPDATE_INTERVAL_SEC = 30


# ─── Form model ───────────────────────────────────────────────────────────────
class BookingForm:
    def __init__(
        self,
        advisor_id     : str = Form(...),
        advisor_name   : str = Form(...),
        date           : str = Form(...),
        time           : str = Form(...),
        research_topic : str = Form(...),
        research_detail: str = Form(...),
    ):
        self.advisor_id      = advisor_id
        self.advisor_name    = advisor_name
        self.date            = date
        self.time            = time
        self.research_topic  = research_topic
        self.research_detail = research_detail


# ─── DB helpers ───────────────────────────────────────────────────────────────
def get_db():
    return Connect_MongoDB()["BORC"]


def ensure_booking_indexes(db):
    """สร้าง index ครั้งเดียวตอน startup หรือเรียกจาก lifespan"""
    col = db["BookingOnline"]
    col.create_index([("UserId",    ASCENDING), ("Status", ASCENDING)])
    col.create_index([("Status",    ASCENDING), ("Date",   ASCENDING)])
    col.create_index([("AdvisorId", ASCENDING), ("Date",   ASCENDING), ("Status", ASCENDING)])
    
    col.create_index([("UserId", ASCENDING), ("CreatedAt", DESCENDING)])

    # index สำหรับ query ที่ยิงบ่อย: verify_user_token ค้น UserProfile ด้วย userId ทุก request
    # (ก่อนหน้านี้ไม่มี index จึงเป็น collection scan) และ history ที่ค้นด้วย id ของผู้ใช้/คิว
    db["UserProfile"].create_index([("userId", ASCENDING)])
    db["RescheduleHistory"].create_index([("bookingId", ASCENDING), ("rescheduledById", ASCENDING)])
    db["RescheduleHistory"].create_index([("studentId", ASCENDING)])
    db["ApprovedHistory"].create_index([("UserId", ASCENDING)])
    db["CancelBookingHistory"].create_index([("cancelledById", ASCENDING)])
    db["QueueManagementHistory"].create_index([("userId", ASCENDING), ("status", ASCENDING)])
    # AdvisorStats นับด้วย AdvisorId / advisorName
    db["ApprovedHistory"].create_index([("AdvisorId", ASCENDING), ("Status", ASCENDING)])
    db["RescheduleHistory"].create_index([("advisorName", ASCENDING)])
    db["CancelBookingHistory"].create_index([("advisorName", ASCENDING)])

    lock_col = db["_AutoUpdateLock"]
    
    # ─── แก้ไขตรงส่วนนี้ ───
    try:
        lock_col.create_index([("key", ASCENDING)], unique=True, name="auto_update_key_unique")
    except DuplicateKeyError:
        # หากมีข้อมูล key: "auto_update" ซ้ำกันอยู่ ให้ลบข้อมูลใน _AutoUpdateLock ทิ้ง
        lock_col.delete_many({}) 
        # แล้วลองสร้าง Index ใหม่อีกครั้ง
        lock_col.create_index([("key", ASCENDING)], unique=True, name="auto_update_key_unique")
        
    lock_col.update_one(
        {"key": "auto_update"},
        {"$setOnInsert": {"lastRun": datetime(1970, 1, 1, tzinfo=timezone.utc)}},
        upsert=True,
    )


# ─── Slot helpers ─────────────────────────────────────────────────────────────
def parse_time_range(time_str: str) -> tuple[str, str]:
    """แยก '09:00-10:00' → ('09:00', '10:00')"""
    parts = time_str.split("-")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="รูปแบบเวลาไม่ถูกต้อง (HH:MM-HH:MM)")
    return parts[0].strip(), parts[1].strip()


def find_slot_doc_for_date(db, advisor_id: str, date: str):
    """ค้นหา document slot ของอาจารย์ในวันที่ระบุ"""
    return db["ManageTimeSlots"].find_one({
        "advisorId"     : advisor_id,
        f"dates.{date}" : {"$exists": True},
    })


def save_auto_queue_history(db, booking: dict, status: str, reason: str, now_utc: datetime):
    """ให้ทั้งนักศึกษาและอาจารย์เห็นรายการที่ระบบเปลี่ยนสถานะอัตโนมัติ."""
    events = [
        (booking.get("AdvisorId", ""), booking.get("StudentName", "")),
        (booking.get("UserId", ""), booking.get("Advisor_Name", "")),
    ]
    for user_id, user_name in events:
        if user_id:
            db["QueueManagementHistory"].insert_one({
                "userId"    : user_id,
                "role"      : "System",
                "UserName"  : user_name,
                "status"    : status,
                "Reason"    : reason,
                "createdAt" : now_utc,
                "updatedAt" : now_utc,
            })


# ─── Auto update status ───────────────────────────────────────────────────────
def auto_update_status(db):
    """
    เรียกได้ทั้งจาก request และ background worker
    ใช้ Distributed Lock ใน MongoDB เพื่อให้รันจริงเพียง process เดียวในแต่ละรอบ
    """
    col_lock = db["_AutoUpdateLock"]
    now_utc  = datetime.now(timezone.utc)
    # รองรับกรณี worker เชื่อมต่อฐานข้อมูลได้ภายหลัง startup
    col_lock.update_one(
        {"key": "auto_update"},
        {"$setOnInsert": {"lastRun": datetime(1970, 1, 1, tzinfo=timezone.utc)}},
        upsert=True,
    )

    lock_result = col_lock.update_one(
        {
            "key": "auto_update",
            "lastRun": {"$lt": now_utc - timedelta(seconds=AUTO_UPDATE_INTERVAL_SEC)},
        },
        {"$set": {"lastRun": now_utc}},
    )
    if lock_result.modified_count == 0:
        return  # มี process อื่นรันอยู่แล้ว

    _do_auto_update(db, now_utc)


def _do_auto_update(db, now_utc: datetime):
    tz_utc7  = timezone(timedelta(hours=7))
    now_utc7 = now_utc.astimezone(tz_utc7)
    col      = db["BookingOnline"]

    # หมายเหตุ: ไม่รวม "Rescheduled" — ต้องรอให้อาจารย์ Approve รอบใหม่ก่อน
    # ระบบจะไม่ดัน Rescheduled → InProgress โดยอัตโนมัติ แต่จะ Cancel ถ้าหมดเวลา
    bookings = list(col.find({
        "Status": {"$in": ["Pending", "Approved", "InProgress", "Rescheduled"]}
    }))

    for booking in bookings:
        try:
            _process_booking_status(db, col, booking, now_utc, now_utc7, tz_utc7)
        except Exception as exc:
            logger.warning(f"[WARN] auto status update failed for booking {booking.get('_id')}: {exc}")
            continue


def _process_booking_status(db, col, booking, now_utc, now_utc7, tz_utc7):
    """
    กฎการเปลี่ยน Status อัตโนมัติ:
      Pending/Rescheduled + ก่อนเวลาเริ่ม 1 ชั่วโมง (ยังไม่ Approve) → Cancelled
      Approved   + ถึงเวลาเริ่ม  → InProgress
      InProgress + เลยเวลาสิ้นสุด  → Completed
    """
    date_str   = booking.get("Date", "")
    time_str   = booking.get("Time", "")
    status     = booking.get("Status", "")
    advisor_id = booking.get("AdvisorId", "")

    if not date_str or not time_str or "-" not in time_str:
        return

    start = time_str.split("-")[0].strip()
    end   = time_str.split("-")[1].strip()

    try:
        start_dt = datetime.strptime(f"{date_str} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=tz_utc7)
        end_dt   = datetime.strptime(f"{date_str} {end}",   "%Y-%m-%d %H:%M").replace(tzinfo=tz_utc7)
    except ValueError:
        return

    # ─── ยังไม่อนุมัติเมื่อเข้าสู่ช่วงก่อนนัด 1 ชั่วโมง → Cancelled ───────────────────────
    # ใช้ filter สถานะซ้ำอีกครั้ง เพื่อไม่ทับการกดอนุมัติที่เกิดพร้อมกัน
    approval_deadline = start_dt - timedelta(hours=CUTOFF_HOURS)
    if status in ("Pending", "Rescheduled") and now_utc7 >= approval_deadline:
        cancelled = col.update_one(
            {"_id": booking["_id"], "Status": {"$in": ["Pending", "Rescheduled"]}},
            {"$set": {"Status": "Cancelled", "UpdatedAt": now_utc}}
        )
        if cancelled.modified_count:
            db["CancelBookingHistory"].insert_one({
                "cancelledById"  : "system",
                "cancelledByRole": "System",
                "advisorName"    : booking.get("Advisor_Name", ""),
                "studentName"    : booking.get("StudentName", ""),
                "cancelledDate"  : date_str,
                "cancelReason"   : "อาจารย์ไม่ได้ยืนยันคิวก่อนถึงเวลานัด 1 ชั่วโมง",
                "status"         : "Cancelled",
                "createdAt"      : now_utc,
                "updatedAt"      : now_utc,
            })
            save_auto_queue_history(
                db,
                booking,
                "Cancelled",
                "อาจารย์ไม่ได้ยืนยันคิวก่อนถึงเวลานัด 1 ชั่วโมง",
                now_utc,
            )
            recalculate_slot_booked(db, advisor_id, date_str, start, end)
            # แจ้งเตือนนักศึกษาว่าคิวถูกระบบยกเลิกอัตโนมัติ (เดิมไม่มีการแจ้งเตือนเลย
            # ทำให้นักศึกษาไม่รู้ว่าคิวถูกยกเลิกจนกว่าจะเปิดแอปเอง)
            notify_chatbot(f"{chatbot_uri}/NotifyQueueStudent/NotifyStudent", {
                "userId"      : booking.get("UserId", ""),
                "StudentName" : booking.get("StudentName", ""),
                "AdvisorName" : booking.get("Advisor_Name", ""),
                "Date"        : date_str,
                "Time"        : time_str,
                "Status"      : "Cancelled",
            }, CHATBOT_INTERNAL_HEADERS)
        return

    # ─── Approved + ถึงเวลาเริ่ม → InProgress ───────────────────────────────
    # (Rescheduled ไม่รวม — ต้องให้อาจารย์ Approve รอบใหม่ก่อนเสมอ)
    if status == "Approved" and now_utc7 >= start_dt:
        started = col.update_one(
            {"_id": booking["_id"], "Status": "Approved"},
            {"$set": {"Status": "InProgress", "UpdatedAt": now_utc}}
        )
        if started.modified_count:
            recalculate_slot_booked(db, advisor_id, date_str, start, end)
        return

    # ─── InProgress + เลยเวลาสิ้นสุด → Completed ───────────────────────────
    if status == "InProgress" and now_utc7 >= end_dt:
        completed = col.update_one(
            {"_id": booking["_id"], "Status": "InProgress"},
            {"$set": {
                "Status"     : "Completed",
                "CompletedAt": now_utc,
                "UpdatedAt"  : now_utc,
            }}
        )
        if completed.modified_count:
            save_auto_queue_history(
                db,
                booking,
                "Completed",
                "ระบบปิดการให้คำปรึกษาอัตโนมัติเมื่อสิ้นสุดเวลานัด",
                now_utc,
            )
            recalculate_slot_booked(db, advisor_id, date_str, start, end)



# ─── กฎ "slot จองได้หรือไม่" (ใช้ร่วมกันโดย AvailableAdvisors / AvailableSlots) ───────────
def _slot_is_taken(slot: dict) -> bool:
    """slot ถูกล็อก ปิด หรือเต็มแล้ว (จองไม่ได้)"""
    return (
        slot.get("isLocked", False)
        or slot.get("is_closed", False)
        or slot.get("booked", 0) >= slot.get("max_booking", 1)
    )


def _cutoff_datetime(date: str, start: str) -> datetime | None:
    """เวลาปิดรับจอง = เวลาเริ่ม - CUTOFF_HOURS (UTC+7); None ถ้าเวลาเริ่มผิดรูปแบบ"""
    try:
        start_dt = datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=7)))
    except ValueError:
        return None
    return start_dt - timedelta(hours=CUTOFF_HOURS)


def _has_bookable_slot(slots: list, date: str, today: str, now: datetime) -> bool:
    """มี slot ที่จองได้อย่างน้อย 1 ช่วง — ถ้าเป็นวันนี้ ต้องยังไม่เลยเวลา cutoff

    (เวลาเริ่มที่ผิดรูปแบบของวันนี้ ไม่ถูกนับว่าว่าง: พฤติกรรมเดิมของรายชื่ออาจารย์)
    """
    for s in slots:
        if _slot_is_taken(s):
            continue
        if date != today:
            return True
        cutoff = _cutoff_datetime(date, s["start"])
        if cutoff is not None and now < cutoff:
            return True
    return False


def _slot_view(s: dict, date: str, today: str, now: datetime) -> dict:
    """รูปแบบ slot ที่ส่งให้หน้าจอง พร้อมสถานะปิด/เต็ม/เลยเวลา

    (เวลาเริ่มที่ผิดรูปแบบของวันนี้ ถือว่ายังไม่เลยเวลา: พฤติกรรมเดิมของหน้าเลือกเวลา)
    """
    is_past = False
    if date == today:
        cutoff = _cutoff_datetime(date, s["start"])
        is_past = cutoff is not None and now >= cutoff
    return {
        "start"    : s["start"],
        "end"      : s["end"],
        "label"    : s.get("label", ""),
        "is_closed": _slot_is_taken(s) or is_past,
        "is_past"  : is_past,
        "is_booked": s.get("isLocked", False) or s.get("booked", 0) >= s.get("max_booking", 1),
    }


# ─── GET /AvailableAdvisors ───────────────────────────────────────────────────
@router.get("/AvailableAdvisors")
def get_available_advisors(request: Request):
    """ดึงรายชื่ออาจารย์ที่มี slot ว่างตั้งแต่วันนี้เป็นต้นไป"""
    try:
        verify_user_token(request)
        db       = get_db()
        now_utc7 = get_now_utc7()
        today    = now_utc7.strftime("%Y-%m-%d")

        all_docs = list(db["ManageTimeSlots"].find(
            {},
            {"_id": 0, "advisor_name": 1, "advisorId": 1, "dates": 1}
        ))

        advisors: dict = {}
        for doc in all_docs:
            advisor_id = doc.get("advisorId", "")
            name       = doc.get("advisor_name", "")
            if not advisor_id or not name:
                continue

            entry = advisors.setdefault(advisor_id, {"name": name, "has_available": False})
            if entry["has_available"]:
                continue
            entry["has_available"] = any(
                isinstance(slots, list) and date >= today and _has_bookable_slot(slots, date, today, now_utc7)
                for date, slots in doc.get("dates", {}).items()
            )

        return {"advisors": [
            {"advisor_id": aid, "advisor_name": info["name"]}
            for aid, info in advisors.items()
            if info["has_available"]
        ]}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /AvailableSlots/{advisor_id} ────────────────────────────────────────
@router.get("/AvailableSlots/{advisor_id}")
def get_available_slots(advisor_id: str, request: Request):
    """ดึง slot ทั้งหมดของอาจารย์ พร้อมสถานะว่าง/ถูกจอง"""
    try:
        verify_user_token(request)
        db       = get_db()
        now_utc7 = get_now_utc7()
        today    = now_utc7.strftime("%Y-%m-%d")

        docs = list(db["ManageTimeSlots"].find(
            {"advisorId": advisor_id},
            {"_id": 0, "dates": 1, "advisorId": 1}
        ))
        if not docs:
            return {"dates": {}}

        merged_dates = {}
        for doc in docs:
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list) or date < today:
                    continue
                merged_dates[date] = [_slot_view(s, date, today, now_utc7) for s in slots]

        return {"advisor_id": advisor_id, "dates": merged_dates}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Background notify helper ─────────────────────────────────────────────────
def _notify_advisor_background(chatbot_url: str, payload: dict):
    """ส่งแจ้งเตือนอาจารย์ใน background thread ไม่บล็อก response"""
    try:
        req.post(chatbot_url, json=payload, timeout=8, headers=CHATBOT_INTERNAL_HEADERS)
    except Exception as e:
        logger.warning(f"[WARN] แจ้งเตือน Advisor ล้มเหลว: {e}")


# ─── POST /BookingOnline helpers ──────────────────────────────────────────────
def _validate_attachment(file: UploadFile | None) -> str | None:
    """ตรวจไฟล์แนบ (PDF/DOCX ≤ 10MB) คืนชื่อไฟล์ หรือ None ถ้าไม่มีไฟล์"""
    if not (file and file.filename):
        return None
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="อนุญาตเฉพาะไฟล์ PDF หรือ DOCX เท่านั้น")
    # def ธรรมดา (รันใน threadpool) จึงอ่านผ่าน file.file แบบ sync ได้
    contents = file.file.read()
    if len(contents) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"ขนาดไฟล์ต้องไม่เกิน 10 MB (ไฟล์นี้ {len(contents)/1024/1024:.2f} MB)"
        )
    return file.filename


def _get_latest_booking_or_block(db, user_id: str) -> tuple[dict | None, str | None]:
    """คืน (booking ล่าสุด, status) ของผู้ใช้; raise ถ้ายังมีคิวที่ไม่เสร็จสิ้น"""
    latest_booking = db["BookingOnline"].find_one(
        {"UserId": user_id},
        sort=[("CreatedAt", -1)]
    )
    latest_status = latest_booking.get("Status", "") if latest_booking else None

    if latest_status in BLOCK_BOOKING_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"คุณมีการจองที่ยังไม่เสร็จสิ้น "
                f"(วันที่ {latest_booking['Date']} เวลา {latest_booking['Time']})"
            ),
        )
    return latest_booking, latest_status


def _find_bookable_slot(db, data) -> tuple[dict, str, str, str]:
    """ตรวจวันที่/เวลา/slot ที่เลือก คืน (slot_doc, start, end, advisor_name); raise ถ้าจองไม่ได้"""
    min_date = get_now_utc7().strftime("%Y-%m-%d")
    if data.date < min_date:
        raise HTTPException(
            status_code=400,
            detail=f"กรุณาเลือกวันนี้ ({min_date}) หรือหลังจากนั้น"
        )

    slot_doc = find_slot_doc_for_date(db, data.advisor_id, data.date)
    if not slot_doc:
        raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาของอาจารย์ในวันที่เลือก")

    start, end = parse_time_range(data.time)

    try:
        start_dt = datetime.strptime(f"{data.date} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=7)))
        cutoff_dt = start_dt - timedelta(hours=CUTOFF_HOURS)
        if get_now_utc7() >= cutoff_dt:
            raise HTTPException(status_code=400, detail="ไม่สามารถจองได้ เนื่องจากต้องจองล่วงหน้าอย่างน้อย 1 ชั่วโมง หรือช่วงเวลานี้ผ่านไปแล้ว")
    except ValueError:
        pass

    slot_list   = slot_doc.get("dates", {}).get(data.date, [])
    target_slot = next(
        (s for s in slot_list if s["start"] == start and s["end"] == end), None
    )
    if not target_slot:
        raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาที่เลือก")

    if (
        target_slot.get("isLocked", False)
        or target_slot.get("is_closed", False)
        or target_slot.get("booked", 0) >= target_slot.get("max_booking", 1)
    ):
        raise HTTPException(status_code=400, detail="ช่วงเวลานี้ถูกจองแล้ว กรุณาเลือกช่วงเวลาอื่น")

    # ใช้ชื่อจากตารางของอาจารย์เสมอ ไม่เชื่อชื่อที่ส่งมาจาก browser
    advisor_name = slot_doc.get("advisor_name", "").strip()
    if not advisor_name:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลชื่ออาจารย์")

    return slot_doc, start, end, advisor_name


def _lock_slot(db, slot_doc: dict, date: str, start: str, end: str) -> None:
    """Atomic lock slot (ป้องกัน race condition / double booking); raise ถ้ามีคนจองตัดหน้า"""
    inc_result = db["ManageTimeSlots"].update_one(
        {"_id": slot_doc["_id"]},
        {
            "$inc": {f"dates.{date}.$[slot].booked": 1},
            "$set": {
                f"dates.{date}.$[slot].isLocked" : True,
                f"dates.{date}.$[slot].is_closed": True,
            },
        },
        array_filters=[{
            "slot.start"   : start,
            "slot.end"     : end,
            "slot.isLocked": False,
            "slot.is_closed": {"$ne": True},
            "slot.booked"  : 0,
        }]
    )
    if inc_result.modified_count == 0:
        raise HTTPException(status_code=400, detail="ช่วงเวลานี้ถูกจองแล้ว กรุณาเลือกช่วงเวลาอื่น")


def _get_student_name(db, user_id: str) -> str:
    profile = db["UserProfile"].find_one(
        {"userId": user_id},
        {"_id": 0, "Prefix": 1, "Firstname": 1, "Lastname": 1}
    )
    if not profile:
        return ""
    return (
        f"{profile.get('Prefix', '')}"
        f"{profile.get('Firstname', '')} "
        f"{profile.get('Lastname', '')}".strip()
    )


def _save_booking(db, booking_payload: dict, latest_booking: dict | None, latest_status: str | None) -> None:
    """Approved/Rescheduled → update document เดิม; Cancelled / Completed / ใหม่ → insert ใหม่เสมอ"""
    is_fresh = latest_status in (None, "Cancelled", "Completed")
    booking_to_update = (
        None if is_fresh
        else (latest_booking if latest_status in UPDATE_BOOKING_STATUSES else None)
    )

    if booking_to_update is not None:
        db["BookingOnline"].update_one(
            {"_id": booking_to_update["_id"]},
            {"$set": booking_payload}
        )
    else:
        db["BookingOnline"].insert_one(booking_payload)


# ─── POST /BookingOnline ──────────────────────────────────────────────────────
@router.post("/BookingOnline")
def create_booking(
    request         : Request,
    background_tasks: BackgroundTasks,
    data            : BookingForm = Depends(),
    file            : UploadFile  = File(None),
):
    """
    จอง slot กับอาจารย์ที่ปรึกษา
    - ตรวจสอบไฟล์แนบ (PDF/DOCX ≤ 10MB)
    - บล็อกถ้าผู้ใช้มีการจองที่ยังไม่เสร็จ (Pending/InProgress)
    - ล็อก slot ด้วย atomic update เพื่อป้องกัน double booking
    """
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)

        file_path = _validate_attachment(file)

        db = get_db()
        auto_update_status(db)

        latest_booking, latest_status = _get_latest_booking_or_block(db, user_id)
        slot_doc, start, end, advisor_name = _find_bookable_slot(db, data)
        _lock_slot(db, slot_doc, data.date, start, end)

        student_name = _get_student_name(db, user_id)

        now = datetime.now(timezone.utc)
        _save_booking(
            db,
            {
                "UserId"                : user_id,
                "StudentName"           : student_name,
                "AdvisorId"             : data.advisor_id,
                "Advisor_Name"          : advisor_name,
                "Date"                  : data.date,
                "Time"                  : data.time,
                "ResearchTopic"         : data.research_topic,
                "ResearchDetail"        : data.research_detail,
                "FilePath"              : file_path,
                "Status"                : "Pending",
                "RescheduledOnce"       : False,
                "AdvisorRescheduledOnce": False,
                "CreatedAt"             : now,
                "UpdatedAt"             : now,
            },
            latest_booking,
            latest_status,
        )

        # แจ้งเตือนอาจารย์มีนักศึกษามาจอง (ทำงานใน background ไม่บล็อก response)
        background_tasks.add_task(
            _notify_advisor_background,
            f"{chatbot_uri}/NotifyQueueAdivsor/BookingStudent",
            {
                "AdvisorId"    : data.advisor_id,
                "StudentName"  : student_name,
                "ResearchTopic": data.research_topic,
                "Date"         : data.date,
                "Time"         : data.time,
                "Status"       : "Pending"
            },
        )

        return {"message": "เสร็จสิ้นการจองคิวให้คำปรึกษา"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /BookingStatus ───────────────────────────────────────────────────────
@router.get("/BookingStatus")
def get_booking_status(request: Request):
    """
    ดูสถานะการจองล่าสุดของผู้ใช้
    - ถ้า active → คืนข้อมูล booking + can_book: False
    - ถ้า Completed หรือ Cancelled → should_reset: True เพื่อให้ frontend reset UI
    - ถ้าไม่มี   → can_book: True
    """
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        db      = get_db()

        # ใช้ CreatedAt -1 เพื่อให้สอดคล้องกับ /BookingOnline POST ที่เช็คคิวล่าสุด
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id},
            sort=[("CreatedAt", -1)]
        )
        if not booking:
            return {"status": None, "should_reset": False, "can_book": True}

        status    = booking.get("Status", "")
        is_active = status in ACTIVE_STATUSES

        return {
            "status"      : status,
            "should_reset": status in {"Completed", "Cancelled"},
            "can_book"    : not is_active,
            "booking"     : {
                "AdvisorId"    : booking.get("AdvisorId", ""),
                "Advisor_Name" : booking.get("Advisor_Name", ""),
                "Date"         : booking.get("Date", ""),
                "Time"         : booking.get("Time", ""),
                "ResearchTopic": booking.get("ResearchTopic", ""),
                "Status"       : status,
            } if is_active else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── POST /SyncAllSlots ───────────────────────────────────────────────────────
@router.post("/SyncAllSlots")
def sync_all_slots(request: Request):
    """
    [Admin/Advisor only]
    Force-sync booked count และ isLocked ของทุก slot
    ใช้เมื่อข้อมูล slot ไม่ตรงกับ booking จริง
    """
    try:
        payload = verify_user_token(request)
        if payload.get("role") not in ("Admin", "Advisor"):
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")

        db        = get_db()
        col_slots = db["ManageTimeSlots"]
        updated   = 0

        for doc in col_slots.find({}, {"advisorId": 1, "dates": 1}):
            advisor_id = doc.get("advisorId", "")
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list):
                    continue
                for s in slots:
                    start = s.get("start", "")
                    end   = s.get("end",   "")
                    if start and end:
                        recalculate_slot_booked(db, advisor_id, date, start, end)
                        updated += 1

        return {"message": f"Sync เสร็จสิ้น อัปเดต {updated} slots"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

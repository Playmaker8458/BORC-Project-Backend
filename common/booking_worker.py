"""Background worker: เปลี่ยนสถานะคิวอัตโนมัติตามเวลานัด

กฎ (เวลาไทย UTC+7):
  Pending/Rescheduled + ถึงเวลา (นัด - CUTOFF_HOURS) โดยอาจารย์ยังไม่อนุมัติ → Cancelled
                                            (+ประวัติ, คืน slot, แจ้งนักศึกษาผ่าน ChatBot)
  Approved   + ถึงเวลาเริ่ม                 → InProgress  (slot ยังล็อก)
  InProgress + เลยเวลาสิ้นสุด               → Completed   (+ประวัติ, คืน slot)

ทุกการเปลี่ยนสถานะใช้ filter สถานะเดิมซ้ำตอนเขียน เพื่อไม่ทับการกระทำที่เกิดพร้อมกัน (เช่น อาจารย์กดอนุมัติ)
เดิมอยู่ใน users/router/Students/BookingOnline.py ปนกับ API การจอง — แยกออกมาเพราะเป็นงานเบื้องหลังที่ไม่เกี่ยวกับ HTTP
"""

import logging
from datetime import datetime, timedelta, timezone

from common.booking_status import ACTIVE_STATUSES
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.slot_service import CUTOFF_HOURS, recalculate_slot_booked

logger = logging.getLogger(__name__)

chatbot_uri = CHATBOT_URL

# Background worker ตรวจทุก 30 วินาที จึงเปลี่ยนสถานะใกล้เวลาจริงได้
AUTO_UPDATE_INTERVAL_SEC = 30


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


def auto_update_status(db):
    """
    เรียกจาก background worker (main.booking_status_worker) ทุก 30 วินาที
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

    run_auto_update(db, now_utc)


def run_auto_update(db, now_utc: datetime):
    """ตรวจคิวที่ active ทั้งหมดหนึ่งรอบ แล้วเปลี่ยนสถานะตามกฎด้านล่าง (ไม่ผ่านล็อก — ใช้ auto_update_status ใน production)"""
    tz_utc7  = timezone(timedelta(hours=7))
    now_utc7 = now_utc.astimezone(tz_utc7)
    col      = db["BookingOnline"]

    # Rescheduled อยู่ในรายการเพื่อให้ถูกยกเลิกเมื่อไม่มีใครอนุมัติก่อนนัด แต่จะไม่ถูกดันเป็น InProgress
    # เอง — ต้องรอให้อาจารย์ Approve เวลาใหม่ก่อนเสมอ (ดู _process_booking_status)
    bookings = list(col.find({"Status": {"$in": ACTIVE_STATUSES}}))

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

    if not date_str or not time_str or "-" not in time_str:
        return

    start = time_str.split("-")[0].strip()
    end   = time_str.split("-")[1].strip()

    try:
        start_dt = datetime.strptime(f"{date_str} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=tz_utc7)
        end_dt   = datetime.strptime(f"{date_str} {end}",   "%Y-%m-%d %H:%M").replace(tzinfo=tz_utc7)
    except ValueError:
        return

    approval_deadline = start_dt - timedelta(hours=CUTOFF_HOURS)
    if status in ("Pending", "Rescheduled") and now_utc7 >= approval_deadline:
        _auto_cancel_unapproved(db, col, booking, now_utc, start, end)
    # (Rescheduled ไม่รวม — ต้องให้อาจารย์ Approve รอบใหม่ก่อนเสมอ)
    elif status == "Approved" and now_utc7 >= start_dt:
        _auto_start(db, col, booking, now_utc, start, end)
    elif status == "InProgress" and now_utc7 >= end_dt:
        _auto_complete(db, col, booking, now_utc, start, end)


def _auto_cancel_unapproved(db, col, booking, now_utc, start, end):
    """ยังไม่อนุมัติเมื่อเข้าสู่ช่วงก่อนนัด 1 ชั่วโมง → Cancelled

    ใช้ filter สถานะซ้ำอีกครั้ง เพื่อไม่ทับการกดอนุมัติที่เกิดพร้อมกัน
    """
    date_str   = booking.get("Date", "")
    advisor_id = booking.get("AdvisorId", "")
    cancelled = col.update_one(
        {"_id": booking["_id"], "Status": {"$in": ["Pending", "Rescheduled"]}},
        {"$set": {"Status": "Cancelled", "UpdatedAt": now_utc}}
    )
    if not cancelled.modified_count:
        return
    reason = "อาจารย์ไม่ได้ยืนยันคิวก่อนถึงเวลานัด 1 ชั่วโมง"
    db["CancelBookingHistory"].insert_one({
        "cancelledById"  : "system",
        "cancelledByRole": "System",
        "advisorId"      : advisor_id,
        "advisorName"    : booking.get("Advisor_Name", ""),
        "studentName"    : booking.get("StudentName", ""),
        "cancelledDate"  : date_str,
        "cancelReason"   : reason,
        "status"         : "Cancelled",
        "createdAt"      : now_utc,
        "updatedAt"      : now_utc,
    })
    save_auto_queue_history(db, booking, "Cancelled", reason, now_utc)
    recalculate_slot_booked(db, advisor_id, date_str, start, end)
    # แจ้งเตือนนักศึกษาว่าคิวถูกระบบยกเลิกอัตโนมัติ (เดิมไม่มีการแจ้งเตือนเลย
    # ทำให้นักศึกษาไม่รู้ว่าคิวถูกยกเลิกจนกว่าจะเปิดแอปเอง)
    notify_chatbot(f"{chatbot_uri}/NotifyQueueStudent/NotifyStudent", {
        "userId"      : booking.get("UserId", ""),
        "StudentName" : booking.get("StudentName", ""),
        "AdvisorName" : booking.get("Advisor_Name", ""),
        "Date"        : date_str,
        "Time"        : booking.get("Time", ""),
        "Status"      : "Cancelled",
    }, CHATBOT_INTERNAL_HEADERS)


def _auto_start(db, col, booking, now_utc, start, end):
    """Approved + ถึงเวลาเริ่ม → InProgress"""
    started = col.update_one(
        {"_id": booking["_id"], "Status": "Approved"},
        {"$set": {"Status": "InProgress", "UpdatedAt": now_utc}}
    )
    if started.modified_count:
        recalculate_slot_booked(db, booking.get("AdvisorId", ""), booking.get("Date", ""), start, end)


def _auto_complete(db, col, booking, now_utc, start, end):
    """InProgress + เลยเวลาสิ้นสุด → Completed"""
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
        recalculate_slot_booked(db, booking.get("AdvisorId", ""), booking.get("Date", ""), start, end)

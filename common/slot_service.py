"""Logic กลางสำหรับ sync สถานะ slot (ManageTimeSlots) กับ booking จริง (BookingOnline)

รวมมาจาก users/router/Students/BookingOnline.py, Reschedule_Students.py,
Advisor/ManageQueueAdvisor.py, Advisor/Rechedule_Advisor.py ซึ่งแต่ละไฟล์เคย
มีสำเนาของ logic เดียวกันนี้แยกกัน ทำให้แก้บั๊กที่จุดเดียวไม่ครบทุกที่ได้ง่าย
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# นักศึกษามีนัดที่ยังใช้งานอยู่ได้เพียงหนึ่งรายการ; ใช้เช็คว่า slot ควรถูกล็อกหรือไม่
ACTIVE_STATUSES = ["Pending", "Approved", "InProgress", "Rescheduled"]
SLOT_BLOCKING_STATUSES = ACTIVE_STATUSES

# Cutoff แบบ "ก่อนเวลานัดเท่านั้น" — ใช้ตอนนักศึกษาจอง/ยกเลิก/เลื่อนคิว
CUTOFF_HOURS = 1

# Cutoff แบบ "ก่อน-หลัง" — ใช้ตอนอาจารย์ยกเลิก/เลื่อนคิว (ล็อกทั้งก่อนและหลังเวลานัด 1 ชม.)
CUTOFF_HOURS_BEFORE = 1
CUTOFF_HOURS_AFTER = 1

_TZ_UTC7 = timezone(timedelta(hours=7))


def is_within_cutoff(date_str: str, start_time: str) -> bool:
    """True ถ้าตอนนี้เลยเวลา cutoff แล้ว (นัดหมาย - CUTOFF_HOURS)

    ใช้บล็อกการจอง/ยกเลิก/แก้ไขเมื่อใกล้ถึงเวลานัด (ฝั่งนักศึกษา)
    """
    now_utc7 = datetime.now(timezone.utc).astimezone(_TZ_UTC7)
    try:
        start_dt = datetime.strptime(
            f"{date_str} {start_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=_TZ_UTC7)
        cutoff_dt = start_dt - timedelta(hours=CUTOFF_HOURS)
        return now_utc7 >= cutoff_dt
    except ValueError:
        return False


def is_within_advisor_cutoff_window(date_str: str, start_time: str) -> bool:
    """True ถ้าอยู่ในช่วง [เวลานัด - CUTOFF_HOURS_BEFORE, เวลานัด + CUTOFF_HOURS_AFTER)

    ใช้บล็อกการยกเลิก/เลื่อนคิวฝั่งอาจารย์ (ต่างจาก is_within_cutoff ตรงที่ล็อก
    ทั้งก่อนและหลังเวลานัด ไม่ใช่แค่ก่อน)
    """
    now_utc7 = datetime.now(timezone.utc).astimezone(_TZ_UTC7)
    try:
        start_dt = datetime.strptime(
            f"{date_str} {start_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=_TZ_UTC7)
        cutoff_before = start_dt - timedelta(hours=CUTOFF_HOURS_BEFORE)
        cutoff_after = start_dt + timedelta(hours=CUTOFF_HOURS_AFTER)
        return cutoff_before <= now_utc7 < cutoff_after
    except ValueError:
        return False


def can_cancel_approved(date_str: str, start_time: str) -> bool:
    """True ถ้ายังไม่ถึงช่วง cutoff ก่อนเวลานัด (ฝั่งอาจารย์ยกเลิกคิวที่ Approved แล้ว)"""
    now_utc7 = datetime.now(timezone.utc).astimezone(_TZ_UTC7)
    try:
        start_dt = datetime.strptime(
            f"{date_str} {start_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=_TZ_UTC7)
        cutoff_before = start_dt - timedelta(hours=CUTOFF_HOURS_BEFORE)
        return now_utc7 < cutoff_before
    except ValueError:
        return False


def recalculate_slot_booked(db, advisor_id: str, date: str, start: str, end: str):
    """นับ booking จริงแล้ว sync กลับไปที่ slot (keyed ด้วย advisorId)

    - ถ้า booking active >= 1 → ล็อก slot
    - ถ้า = 0                → ปลดล็อก slot
    """
    try:
        col_booking = db["BookingOnline"]
        col_slots = db["ManageTimeSlots"]

        real_booked = col_booking.count_documents({
            "AdvisorId": advisor_id,
            "Date": date,
            "Time": f"{start}-{end}",
            "Status": {"$in": SLOT_BLOCKING_STATUSES},
        })
        new_is_locked = real_booked >= 1

        doc = col_slots.find_one(
            {"advisorId": advisor_id, f"dates.{date}": {"$exists": True}},
            {"dates": 1}
        )
        if not doc:
            logger.warning(f"[WARN] recalculate_slot_booked: ไม่พบ doc advisorId={advisor_id} date={date}")
            return None, None

        slots = doc.get("dates", {}).get(date, [])
        target_index = next(
            (i for i, s in enumerate(slots) if s["start"] == start and s["end"] == end),
            None
        )
        if target_index is None:
            logger.warning(f"[WARN] recalculate_slot_booked: ไม่พบ slot {start}-{end} date={date}")
            return None, None

        if new_is_locked:
            col_slots.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    f"dates.{date}.{target_index}.booked": real_booked,
                    f"dates.{date}.{target_index}.isLocked": True,
                    f"dates.{date}.{target_index}.is_closed": True,
                    f"dates.{date}.{target_index}.max_booking": 1,
                }}
            )
        else:
            col_slots.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        f"dates.{date}.{target_index}.booked": 0,
                        f"dates.{date}.{target_index}.isLocked": False,
                        f"dates.{date}.{target_index}.max_booking": 1,
                    },
                    "$unset": {f"dates.{date}.{target_index}.is_closed": ""},
                }
            )

        logger.info(
            "[INFO] recalculate_slot_booked: advisorId=%s %s %s-%s -> booked=%s, isLocked=%s",
            advisor_id, date, start, end, real_booked, new_is_locked,
        )
        return real_booked, new_is_locked

    except Exception as e:
        logger.warning(f"[WARN] recalculate_slot_booked error: {e}")
        return None, None


def update_slot(db, advisor_id: str, date: str, start: str, end: str, action: str):
    """book/release slot เดียวแบบ +1/-1 (ใช้ตอนเลื่อนคิว ซึ่งย้าย slot เก่า→ใหม่)

    action = "book"    → booked+1, isLocked=True,  is_closed=True
    action = "release" → booked-1, isLocked=False, is_closed=False
    """
    try:
        col_slots = db["ManageTimeSlots"]
        doc = col_slots.find_one(
            {"advisorId": advisor_id, f"dates.{date}": {"$exists": True}},
            {"dates": 1}
        )
        if not doc:
            logger.warning(f"[WARN] update_slot: ไม่พบ doc ของ advisorId={advisor_id}")
            return

        slots = doc.get("dates", {}).get(date, [])
        target_index = next(
            (i for i, s in enumerate(slots) if s.get("start") == start and s.get("end") == end),
            None
        )
        if target_index is None:
            logger.warning(f"[WARN] update_slot: ไม่พบ slot {start}-{end} วันที่ {date}")
            return

        if action == "book":
            col_slots.update_one(
                {"advisorId": advisor_id},
                {
                    "$set": {
                        f"dates.{date}.{target_index}.isLocked": True,
                        f"dates.{date}.{target_index}.is_closed": True,
                    },
                    "$inc": {
                        f"dates.{date}.{target_index}.booked": 1,
                    }
                }
            )
            logger.info(f"[INFO] update_slot (book): {advisor_id} {date} {start}-{end} → booked+1, locked")

        elif action == "release":
            current_booked = slots[target_index].get("booked", 0)
            col_slots.update_one(
                {"advisorId": advisor_id},
                {"$set": {
                    f"dates.{date}.{target_index}.booked": max(0, current_booked - 1),
                    f"dates.{date}.{target_index}.isLocked": False,
                    f"dates.{date}.{target_index}.is_closed": False,
                }}
            )
            logger.info(f"[INFO] update_slot (release): {advisor_id} {date} {start}-{end} → booked={max(0, current_booked - 1)}, unlocked")

    except Exception as e:
        logger.warning(f"[WARN] update_slot failed: {e}")

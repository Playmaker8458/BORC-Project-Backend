"""Logic กลางสำหรับ sync สถานะ slot (ManageTimeSlots) กับ booking จริง (BookingOnline)

รวมมาจาก users/router/Students/BookingOnline.py, Reschedule_Students.py,
Advisor/ManageQueueAdvisor.py, Advisor/Rechedule_Advisor.py ซึ่งแต่ละไฟล์เคย
มีสำเนาของ logic เดียวกันนี้แยกกัน ทำให้แก้บั๊กที่จุดเดียวไม่ครบทุกที่ได้ง่าย
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

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


def get_now_utc7() -> datetime:
    return datetime.now(timezone.utc).astimezone(_TZ_UTC7)


def get_today_str() -> str:
    """วันนี้ (UTC+7) รูปแบบ YYYY-MM-DD"""
    return get_now_utc7().strftime("%Y-%m-%d")


def get_tomorrow_str() -> str:
    """วันพรุ่งนี้ (UTC+7) รูปแบบ YYYY-MM-DD"""
    return (get_now_utc7() + timedelta(days=1)).strftime("%Y-%m-%d")


def get_reschedule_available_dates(db, advisor_id: str) -> dict:
    """slot ที่ยังว่างของอาจารย์ตั้งแต่ "พรุ่งนี้" เป็นต้นไป รูปแบบ {date: [slot, ...]}

    ใช้ร่วมกันโดยหน้าเลื่อนคิวของนักศึกษาและอาจารย์ (เดิมเป็นสำเนาโค้ดเดียวกันสองที่)
    """
    min_date = get_tomorrow_str()

    docs = list(db["ManageTimeSlots"].find(
        {"advisorId": advisor_id},
        {"_id": 0, "dates": 1}
    ))

    merged_dates: dict = {}
    for doc in docs:
        for date, slots in doc.get("dates", {}).items():
            if not isinstance(slots, list) or date < min_date:
                continue

            available_slots = []
            for s in slots:
                is_closed = s.get("isLocked", False) or s.get("is_closed", False)
                booked = s.get("booked", 0)
                max_book = s.get("max_booking", 1)
                available = max(0, max_book - booked)

                if is_closed or available <= 0:
                    continue

                available_slots.append({
                    "start": s["start"],
                    "end": s["end"],
                    "label": s.get("label", ""),
                    "available": available,
                    "max_booking": max_book,
                })

            if available_slots:
                merged_dates[date] = available_slots

    return merged_dates


def split_time_range(time_str: str) -> tuple[str, str]:
    """"09:00-10:00" -> ("09:00", "10:00"); รูปแบบไม่ถูกต้อง -> ("", "")"""
    parts = [t.strip() for t in time_str.split("-")] if "-" in time_str else []
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ""


def ensure_slot_open_for_reschedule(db, advisor_id: str, date: str, start: str, end: str) -> None:
    """ตรวจว่า slot ปลายทางที่จะเลื่อนคิวไปยังว่างอยู่ (raise HTTPException ถ้าไม่ว่าง)

    ถ้าไม่พบ document ของอาจารย์ในวันนั้นเลยจะไม่ raise (พฤติกรรมเดิมของทั้งสองหน้าเลื่อนคิว)
    """
    slot_doc = db["ManageTimeSlots"].find_one(
        {"advisorId": advisor_id, f"dates.{date}": {"$exists": True}},
        {"dates": 1},
    )
    if not slot_doc:
        return

    slots = slot_doc.get("dates", {}).get(date, [])
    target = next((s for s in slots if s.get("start") == start and s.get("end") == end), None)
    if not target:
        raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาที่เลือก")
    if target.get("isLocked", False) or target.get("is_closed", False):
        raise HTTPException(status_code=400, detail="ช่วงเวลานี้ปิดให้บริการแล้ว")
    if target.get("booked", 0) >= target.get("max_booking", 1):
        raise HTTPException(status_code=400, detail="ช่วงเวลานี้เต็มแล้ว กรุณาเลือกช่วงเวลาอื่น")


def move_booking_to_slot(
    db,
    booking: dict,
    advisor_id: str,
    new_date: str,
    new_start: str,
    new_end: str,
    old_start: str,
    old_end: str,
    extra_fields: dict,
    now: datetime,
) -> None:
    """ย้าย booking ไปช่วงเวลาใหม่: อัปเดต booking -> คืน slot เก่า -> จอง slot ใหม่

    extra_fields = ฟิลด์เฉพาะฝั่งผู้เลื่อน (นักศึกษา: RescheduledOnce, อาจารย์: AdvisorRescheduledOnce)
    """
    db["BookingOnline"].update_one(
        {"_id": booking["_id"]},
        {"$set": {
            "Date": new_date,
            "Time": f"{new_start}-{new_end}",
            "Status": "Rescheduled",
            **extra_fields,
            "UpdatedAt": now,
        }},
    )

    if advisor_id and old_start and old_end:
        update_slot(db, advisor_id, booking.get("Date", ""), old_start, old_end, action="release")
    if advisor_id:
        update_slot(db, advisor_id, new_date, new_start, new_end, action="book")

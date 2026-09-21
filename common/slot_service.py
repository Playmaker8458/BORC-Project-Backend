"""Logic กลางสำหรับ sync สถานะ slot (ManageTimeSlots) กับ booking จริง (BookingOnline)

รวมมาจาก users/router/Students/BookingOnline.py, Reschedule_Students.py,
Advisor/ManageQueueAdvisor.py, Advisor/Reschedule_Advisor.py ซึ่งแต่ละไฟล์เคย
มีสำเนาของ logic เดียวกันนี้แยกกัน ทำให้แก้บั๊กที่จุดเดียวไม่ครบทุกที่ได้ง่าย
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from common.booking_status import ACTIVE_STATUSES, CANCELLABLE_STATUSES

logger = logging.getLogger(__name__)


# Cutoff แบบ "ก่อนเวลานัดเท่านั้น" — ใช้ตอนนักศึกษาจอง/ยกเลิก/เลื่อนคิว
CUTOFF_HOURS = 1

# Cutoff แบบ "ก่อน-หลัง" — ใช้ตอนอาจารย์ยกเลิก/เลื่อนคิว (ล็อกทั้งก่อนและหลังเวลานัด 1 ชม.)
CUTOFF_HOURS_BEFORE = 1
CUTOFF_HOURS_AFTER = 1

_TZ_UTC7 = timezone(timedelta(hours=7))
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def has_started(date_str: str, start_time: str) -> bool:
    """True ถ้าถึงเวลาเริ่มนัดแล้ว (ใช้ตอนอาจารย์ปิดคิวที่ยัง Approved ระหว่างรอ worker เปลี่ยนเป็น InProgress)"""
    now_utc7 = datetime.now(timezone.utc).astimezone(_TZ_UTC7)
    try:
        start_dt = datetime.strptime(
            f"{date_str} {start_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=_TZ_UTC7)
        return now_utc7 >= start_dt
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
            "Status": {"$in": ACTIVE_STATUSES},  # คิวที่ active ล็อก slot
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

        current = slots[target_index]
        if not new_is_locked and current.get("booked", 0) == 0 and (
            current.get("isLocked", False) or current.get("is_closed", False)
        ):
            # ไม่มีคิว และตัว slot ก็ไม่เคยถูกคิวล็อก (booked=0) แต่ปิดอยู่ = อาจารย์ปิดเอง (SaveDaySchedule)
            # ห้ามปลดล็อก ไม่งั้นการ sync จะเปิด slot ที่อาจารย์ตั้งใจปิดกลับมาให้จอง
            logger.info(
                "[INFO] recalculate_slot_booked: advisorId=%s %s %s-%s ปิดโดยอาจารย์ คงสถานะไว้",
                advisor_id, date, start, end,
            )
            return 0, True

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
    parts = [t.strip() for t in time_str.split("-")] if time_str and "-" in time_str else []
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ""


def ensure_slot_open_for_reschedule(db, advisor_id: str, date: str, start: str, end: str) -> None:
    """ตรวจว่า slot ปลายทางที่จะเลื่อนคิวไปยังมีจริงและว่างอยู่ (raise HTTPException ถ้าไม่ผ่าน)

    วันที่ต้องเป็น YYYY-MM-DD และตั้งแต่พรุ่งนี้เป็นต้นไป (ตรงกับที่หน้าเลือกเวลาแสดง)
    ไม่พบ slot ของอาจารย์ในวันนั้น = 404 (เดิมปล่อยผ่านจนย้ายคิวไปวัน/เวลาที่ไม่มีอยู่จริงได้)
    """
    if not _DATE_RE.match(date or "") or date < get_tomorrow_str():
        raise HTTPException(status_code=400, detail="วันที่ต้องเป็นรูปแบบ YYYY-MM-DD และตั้งแต่พรุ่งนี้เป็นต้นไป")

    slot_doc = db["ManageTimeSlots"].find_one(
        {"advisorId": advisor_id, f"dates.{date}": {"$exists": True}},
        {"dates": 1},
    )
    if not slot_doc:
        raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาของอาจารย์ในวันที่เลือก")

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
) -> bool:
    """ย้าย booking ไปช่วงเวลาใหม่: อัปเดต booking -> คืน slot เก่า -> จอง slot ใหม่

    extra_fields = ฟิลด์เฉพาะฝั่งผู้เลื่อน (นักศึกษา: RescheduledOnce, อาจารย์: AdvisorRescheduledOnce)
    คืน False (ไม่แตะ slot) ถ้าคิวไม่ได้อยู่สถานะ Approved แล้ว เช่น ถูกยกเลิก/เริ่มไปแล้วระหว่างที่ตรวจ
    """
    moved = db["BookingOnline"].update_one(
        {"_id": booking["_id"], "Status": "Approved"},
        {"$set": {
            "Date": new_date,
            "Time": f"{new_start}-{new_end}",
            "Status": "Rescheduled",
            **extra_fields,
            "UpdatedAt": now,
        }},
    )
    if moved.matched_count == 0:
        return False

    if advisor_id and old_start and old_end:
        update_slot(db, advisor_id, booking.get("Date", ""), old_start, old_end, action="release")
    if advisor_id:
        update_slot(db, advisor_id, new_date, new_start, new_end, action="book")
    return True


def mark_booking_cancelled(db, booking: dict, now: datetime, extra_fields: dict | None = None) -> bool:
    """เปลี่ยนสถานะ booking เป็น Cancelled เฉพาะเมื่อยังอยู่สถานะที่ยกเลิกได้

    คืน False ถ้าสถานะเปลี่ยนไปแล้วระหว่างที่ตรวจ (เช่น อาจารย์กำลังปิดคิว/ระบบเริ่มนัด) ผู้เรียกต้อง
    ไม่เขียนประวัติและตอบ 409 — เดิมเขียนทับสถานะใดก็ได้ ทำให้คิวที่ Completed กลายเป็น Cancelled
    extra_fields = ฟิลด์เสริมที่ต้อง $set ตอนยกเลิก (เช่น CancelReason ของนักศึกษา)
    """
    result = db["BookingOnline"].update_one(
        {"_id": booking["_id"], "Status": {"$in": CANCELLABLE_STATUSES}},
        {"$set": {"Status": "Cancelled", "UpdatedAt": now, **(extra_fields or {})}},
    )
    return result.matched_count == 1


def sync_slot_for_booking(db, booking: dict) -> None:
    """sync slot ของ booking ที่เพิ่งถูกยกเลิก (นับ booking จริงใหม่ → ปลดล็อกถ้าไม่เหลือคิว)"""
    start, end = split_time_range(booking.get("Time", ""))
    date       = booking.get("Date", "")
    if booking.get("AdvisorId") and date and start and end:
        recalculate_slot_booked(db, booking["AdvisorId"], date, start, end)



def unavailable_advisor_ids(db, advisor_ids) -> set:
    """advisorId ที่มีโปรไฟล์แต่ไม่ควรรับจอง (ไม่ใช่ Advisor หรือสถานะไม่ใช่ Approved เช่น Suspended)

    อาจารย์ที่ถูกระงับต้องไม่โผล่ในรายชื่อและจองไม่ได้ ส่วน id ที่ไม่มีโปรไฟล์เลยไม่ถูกตัดออก
    (คงพฤติกรรมเดิมสำหรับข้อมูลเก่า) — ตอน Admin ลบอาจารย์ slot ของอาจารย์ถูกลบไปพร้อมกันอยู่แล้ว
    """
    ids = [a for a in set(advisor_ids) if a]
    if not ids:
        return set()
    return {
        p["userId"]
        for p in db["UserProfile"].find(
            {
                "userId": {"$in": ids},
                "$or": [{"Role": {"$ne": "Advisor"}}, {"Status": {"$ne": "Approved"}}],
            },
            {"_id": 0, "userId": 1},
        )
    }


def validate_date_or_400(date: str) -> str:
    """ตรวจว่าเป็นวันที่ YYYY-MM-DD ที่มีอยู่จริง (raise 400) — วันที่ถูกใช้ประกอบเป็น key `dates.{date}` ใน MongoDB
    จึงต้องไม่รับรูปแบบแปลก (มีจุด/อักขระพิเศษ) จากผู้ใช้"""
    if not _DATE_RE.match(date or ""):
        raise HTTPException(status_code=400, detail="รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="วันที่ไม่ถูกต้อง")
    return date


# ─── กฎ "slot จองได้หรือไม่" (ใช้ร่วมกันโดยหน้าจองและหน้าดูช่วงเวลาของนักศึกษา) ───────────
def slot_is_taken(slot: dict) -> bool:
    """slot ถูกล็อก ปิด หรือเต็มแล้ว (จองไม่ได้)"""
    return (
        slot.get("isLocked", False)
        or slot.get("is_closed", False)
        or slot.get("booked", 0) >= slot.get("max_booking", 1)
    )


def cutoff_datetime(date: str, start: str) -> datetime | None:
    """เวลาปิดรับจอง = เวลาเริ่ม - CUTOFF_HOURS (UTC+7); None ถ้าเวลาเริ่มผิดรูปแบบ"""
    try:
        start_dt = datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=_TZ_UTC7)
    except ValueError:
        return None
    return start_dt - timedelta(hours=CUTOFF_HOURS)


def has_bookable_slot(slots: list, date: str, today: str, now: datetime) -> bool:
    """มี slot ที่จองได้อย่างน้อย 1 ช่วง — ถ้าเป็นวันนี้ ต้องยังไม่เลยเวลา cutoff

    (เวลาเริ่มที่ผิดรูปแบบของวันนี้ ไม่ถูกนับว่าว่าง: พฤติกรรมเดิมของรายชื่ออาจารย์)
    """
    for s in slots:
        if slot_is_taken(s):
            continue
        if date != today:
            return True
        cutoff = cutoff_datetime(date, s["start"])
        if cutoff is not None and now < cutoff:
            return True
    return False

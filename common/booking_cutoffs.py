"""กฎเรื่องเวลา cutoff และ "slot จองได้หรือไม่" — แยกออกจาก common/slot_service.py

เดิมไฟล์เดียวรวมทั้งกฎเวลา cutoff, การ sync slot กับ booking จริง (DB write),
และการหา slot ว่างสำหรับเลื่อนคิว ทำให้ยาวเกินไป ส่วนนี้เป็นฟังก์ชันคำนวณเวลา/
เงื่อนไขล้วนๆ ไม่แตะ DB โดยตรง (ยกเว้น unavailable_advisor_ids ที่ query
UserProfile เพื่อกรองรายชื่ออาจารย์ที่จองไม่ได้ ซึ่งใช้คู่กับ has_bookable_slot เสมอ)
"""

from datetime import datetime, timedelta, timezone
import re

from fastapi import HTTPException

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


def get_now_utc7() -> datetime:
    return datetime.now(timezone.utc).astimezone(_TZ_UTC7)


def get_today_str() -> str:
    """วันนี้ (UTC+7) รูปแบบ YYYY-MM-DD"""
    return get_now_utc7().strftime("%Y-%m-%d")


def get_tomorrow_str() -> str:
    """วันพรุ่งนี้ (UTC+7) รูปแบบ YYYY-MM-DD"""
    return (get_now_utc7() + timedelta(days=1)).strftime("%Y-%m-%d")


def split_time_range(time_str: str) -> tuple[str, str]:
    """"09:00-10:00" -> ("09:00", "10:00"); รูปแบบไม่ถูกต้อง -> ("", "")"""
    parts = [t.strip() for t in time_str.split("-")] if time_str and "-" in time_str else []
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ""


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

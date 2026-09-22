"""โมเดลคำขอและกฎเวลาของช่วงให้คำปรึกษา (ManageTimeSlots) — ส่วนที่ไม่เกี่ยวกับ HTTP/DB

แยกออกมาจาก users/router/Advisor/ManageTimeSlots.py เพื่อให้ router เหลือแค่ขั้นตอนของแต่ละ endpoint
และทดสอบกฎเหล่านี้ตรงๆ ได้ (ตรวจรูปแบบเวลา, เวลาซ้อนกัน, การสร้าง/คงสถานะของ slot)
"""

import re
from itertools import combinations
from typing import Annotated, Dict, List

from pydantic import AfterValidator, BaseModel, field_validator, model_validator

# จำนวนช่วงเวลาสูงสุดที่อาจารย์ตั้งได้ต่อวัน (ต้องตรงกับ MAX_SLOTS_PER_DAY ฝั่ง frontend)
MAX_SLOTS_PER_DAY = 3

# ─────────────────────────────────────────
# Models
# ─────────────────────────────────────────
def _normalize_time(v: str) -> str:
    """ตรวจรูปแบบเวลา HH:MM (ทุก 30 นาที) และ normalize เป็น 2 หลัก ("9:00" -> "09:00")

    ใช้กับทุก request ที่รับเวลา เพื่อให้ตรงกับเวลาที่เก็บไว้ (เก็บเป็น "HH:MM" 2 หลักเสมอ)
    """
    # ASCII เท่านั้น + fullmatch: เดิมใช้ ^\d{1,2}:\d{2}$ ซึ่ง \d จับเลข Unicode (เช่น ١٠:٠٠) และ $ ยอมให้มี
    # newline ต่อท้าย จึงเคยเก็บเวลาเสียลงฐานข้อมูล ("10:٠٠", "09:00\n") ที่จับคู่กับคิวไม่ได้
    if not re.fullmatch(r"[0-9]{1,2}:[0-9]{2}", v):
        raise ValueError("รูปแบบเวลาไม่ถูกต้อง (HH:MM)")
    h, m = v.split(":")
    if not (0 <= int(h) <= 23 and int(m) in (0, 30)):
        raise ValueError("เวลาต้องอยู่ระหว่าง 00:00-23:30 และเป็นทุก 30 นาที")
    return f"{int(h):02d}:{m}"


TimeStr = Annotated[str, AfterValidator(_normalize_time)]


def _time_to_minutes(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def _ensure_end_after_start(start: str, end: str) -> None:
    if _time_to_minutes(end) <= _time_to_minutes(start):
        raise ValueError("เวลาสิ้นสุดต้องหลังเวลาเริ่ม")


class TimeSlot(BaseModel):
    start: TimeStr
    end  : TimeStr
    max_booking: int = 1

    class Config:
        extra = "ignore"

    @model_validator(mode="after")
    def validate_end_after_start(self):
        _ensure_end_after_start(self.start, self.end)
        return self

    @field_validator("max_booking")
    @classmethod
    def force_max_booking_one(cls, v: int) -> int:
        return 1


class ManageTimeSlotsRequest(BaseModel):
    dates: Dict[str, List[TimeSlot]]


class UpdateSlotRequest(BaseModel):
    old_start: TimeStr
    old_end  : TimeStr
    new_start: TimeStr
    new_end  : TimeStr

    @model_validator(mode="after")
    def validate_new_end_after_start(self):
        _ensure_end_after_start(self.new_start, self.new_end)
        return self


class DeleteSlotRequest(BaseModel):
    start: TimeStr
    end  : TimeStr


class CopyToAllDaysRequest(BaseModel):
    target_month: str
    slots       : List[TimeSlot]


# ─────────────────────────────────────────
# กฎเวลา/slot
# ─────────────────────────────────────────
def get_label(start: str) -> str:
    hour = int(start.split(":")[0])
    if 6  <= hour < 12: return "MORNING"
    if 12 <= hour < 15: return "NOON"
    if 15 <= hour < 18: return "EVENING"
    return "NIGHT"


def _to_minutes(slot) -> tuple[int, int] | None:
    """(เวลาเริ่ม, เวลาจบ) เป็นนาทีนับจาก 00:00; None ถ้าข้อมูลผิดรูปแบบ (ให้ข้ามช่วงนั้น)"""
    try:
        start = slot["start"] if isinstance(slot, dict) else slot.start
        end   = slot["end"]   if isinstance(slot, dict) else slot.end
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        return sh * 60 + sm, eh * 60 + em
    except (KeyError, AttributeError, ValueError):
        return None


def _conflicts(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """สองช่วงเวลาชนกัน: ทับกันจริง หรือ "ติดกัน" (จบเท่ากับเวลาเริ่มของอีกช่วง นับเป็นทับด้วย)"""
    (s1, e1), (s2, e2) = a, b
    return (s1 < e2 and s2 < e1) or e1 == s2 or e2 == s1


def check_overlap(slots: list) -> bool:
    """True ถ้ามีช่วงเวลาอย่างน้อยสองช่วงชนกัน (เทียบทุกคู่ตามเดิม เพื่อคงผลกับช่วงที่เวลาจบก่อนเวลาเริ่ม
    ซึ่ง TimeSlot ยังไม่ได้ห้ามไว้) จำนวน slot ต่อวันน้อย ต้นทุนการเทียบทุกคู่จึงเล็กน้อย"""
    times = [t for t in map(_to_minutes, slots) if t is not None]
    return any(_conflicts(a, b) for a, b in combinations(times, 2))


def compute_is_locked(booked: int, current_locked: bool) -> bool:
    if booked >= 1:
        return True
    return current_locked


def slot_is_locked(slot: dict) -> bool:
    """slot ถูกล็อก (อาจารย์แก้/ลบไม่ได้) เมื่อมีนักศึกษาจองแล้ว

    การ "ปิด" โดยอาจารย์ใช้แค่ is_closed ไม่นับเป็นการล็อก — ข้อมูลเก่าที่ SaveDaySchedule เคยตั้ง
    isLocked=True คู่กับ is_closed=True (booked=0) จึงถือเป็นแค่ "ปิด" ด้วย
    """
    if slot.get("booked", 0) >= 1:
        return True
    return bool(slot.get("isLocked", False)) and not slot.get("is_closed", False)


def slot_closed_by_advisor(slot: dict) -> bool:
    """อาจารย์ปิด slot นี้เอง (ไม่ใช่ปิดเพราะมีคนจอง — การจองก็ตั้ง is_closed=True ด้วย)"""
    return slot.get("booked", 0) < 1 and bool(slot.get("is_closed", False))


def slot_status(slot: dict) -> str:
    """สถานะที่แสดงในหน้าสรุป: เต็ม (มีคนจอง) / ปิด (อาจารย์ปิด) / ว่าง"""
    if slot.get("booked", 0) >= slot.get("max_booking", 1) or slot_is_locked(slot):
        return "เต็ม"
    if slot.get("is_closed", False):
        return "ปิด"
    return "ว่าง"


def merge_months(existing_doc: dict, new_date_keys) -> list:
    existing_months = set(existing_doc.get("months", [])) if existing_doc else set()
    new_months      = {d[:7] for d in new_date_keys}
    return sorted(existing_months | new_months)


def new_slot(start: str, end: str, booked: int = 0, is_locked: bool = False, is_closed: bool = False) -> dict:
    """เอกสาร slot มาตรฐานที่เก็บใน ManageTimeSlots (max_booking บังคับเป็น 1 เสมอ)"""
    return {
        "label"      : get_label(start),
        "start"      : start,
        "end"        : end,
        "max_booking": 1,
        "booked"     : booked,
        "isLocked"   : is_locked,
        "is_closed"  : is_closed,
    }


def preserving_slot(start: str, end: str, old: dict | None) -> dict:
    """slot ที่บันทึกใหม่ โดยคงจำนวนที่ถูกจองและสถานะล็อก/ปิดของ slot เดิม (ถ้ามี)

    ถ้ามีคนจองแล้ว (booked >= 1) จะล็อกและปิดเสมอ (compute_is_locked)
    """
    booked = old.get("booked", 0) if old else 0
    return new_slot(
        start, end, booked,
        is_locked=slot_is_locked(old) if old else False,
        is_closed=compute_is_locked(booked, old.get("is_closed", False) if old else False),
    )


def is_slot(slot: dict, start: str, end: str) -> bool:
    return slot["start"] == start and slot["end"] == end

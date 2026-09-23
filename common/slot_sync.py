"""Sync สถานะ slot (ManageTimeSlots) กับ booking จริง (BookingOnline) — เขียน DB

แยกออกจาก common/slot_service.py: ส่วนนี้คือจุดเดียวที่เขียนทับ booked/isLocked
ของ slot โดยตรง (นับ booking จริงแล้ว sync กลับ หรือ +1/-1 ตอนเลื่อนคิว)
"""

import logging

from common.booking_cutoffs import split_time_range
from common.booking_status import ACTIVE_STATUSES

logger = logging.getLogger(__name__)


def _find_slot(col_slots, advisor_id: str, date: str, start: str, end: str):
    """หา slot เป้าหมายจากทุก doc ของอาจารย์ (อาจมีหลาย doc ที่มีวันเดียวกัน)

    คืน (doc, index, slot) หรือ (None, None, None) — ผู้เรียกต้อง update ด้วย doc["_id"]
    เดิมใช้ find_one ได้แค่ doc แรก ทำให้ slot ที่อยู่ใน doc อื่นหาไม่เจอ/อัปเดตผิด doc
    """
    for doc in col_slots.find(
        {"advisorId": advisor_id, f"dates.{date}": {"$exists": True}},
        {f"dates.{date}": 1},
    ):
        slots = doc.get("dates", {}).get(date, [])
        if not isinstance(slots, list):
            continue
        for i, s in enumerate(slots):
            if s.get("start") == start and s.get("end") == end:
                return doc, i, s
    return None, None, None


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

        doc, target_index, current = _find_slot(col_slots, advisor_id, date, start, end)
        if doc is None:
            logger.warning(f"[WARN] recalculate_slot_booked: ไม่พบ slot {start}-{end} advisorId={advisor_id} date={date}")
            return None, None

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
        doc, target_index, target = _find_slot(col_slots, advisor_id, date, start, end)
        if doc is None:
            logger.warning(f"[WARN] update_slot: ไม่พบ slot {start}-{end} วันที่ {date}")
            return

        if action == "book":
            col_slots.update_one(
                {"_id": doc["_id"]},
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
            current_booked = target.get("booked", 0)
            col_slots.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    f"dates.{date}.{target_index}.booked": max(0, current_booked - 1),
                    f"dates.{date}.{target_index}.isLocked": False,
                    f"dates.{date}.{target_index}.is_closed": False,
                }}
            )
            logger.info(f"[INFO] update_slot (release): {advisor_id} {date} {start}-{end} → booked={max(0, current_booked - 1)}, unlocked")

    except Exception as e:
        logger.warning(f"[WARN] update_slot failed: {e}")


def sync_slot_for_booking(db, booking: dict) -> None:
    """sync slot ของ booking ที่เพิ่งถูกยกเลิก (นับ booking จริงใหม่ → ปลดล็อกถ้าไม่เหลือคิว)"""
    start, end = split_time_range(booking.get("Time", ""))
    date       = booking.get("Date", "")
    if booking.get("AdvisorId") and date and start and end:
        recalculate_slot_booked(db, booking["AdvisorId"], date, start, end)

"""หา slot ว่างสำหรับหน้าเลื่อนคิว และย้าย/ยกเลิก booking — แยกออกจาก common/slot_service.py"""

from datetime import datetime

from fastapi import HTTPException

from common.booking_cutoffs import _DATE_RE, get_tomorrow_str, is_within_cutoff, split_time_range
from common.booking_status import CANCELLABLE_STATUSES
from common.slot_sync import _find_slot, update_slot


def _open_slots(slots: list) -> list:
    """กรองเฉพาะ slot ที่ยังไม่ปิดและยังมีที่ว่าง แปลงเป็นรูปแบบที่หน้าเลื่อนคิวใช้"""
    result = []
    for s in slots:
        is_closed = s.get("isLocked", False) or s.get("is_closed", False)
        max_book = s.get("max_booking", 1)
        available = max(0, max_book - s.get("booked", 0))
        if is_closed or available <= 0:
            continue
        result.append({
            "start": s["start"],
            "end": s["end"],
            "label": s.get("label", ""),
            "available": available,
            "max_booking": max_book,
        })
    return result


def _same_day_slot(date: str, s: dict, old_start: str) -> dict:
    """แปลง slot ในวันเดิมของคิวเป็นรูปแบบหน้าเลื่อนคิว พร้อม status

    current = เวลาเดิมของคิว, full = มีคนจองเต็มแล้ว, closed = อาจารย์ปิดไว้,
    past = เหลือเวลาไม่ถึง cutoff (1 ชม.) ก่อนเริ่ม, open = เลือกได้
    """
    max_book = s.get("max_booking", 1)
    booked = s.get("booked", 0)
    if s["start"] == old_start:
        status = "current"
    elif booked >= max_book:
        status = "full"
    elif s.get("isLocked", False) or s.get("is_closed", False):
        status = "closed"
    elif is_within_cutoff(date, s["start"]):
        status = "past"
    else:
        status = "open"
    return {
        "start": s["start"],
        "end": s["end"],
        "label": s.get("label", ""),
        "available": max(0, max_book - booked),
        "max_booking": max_book,
        "status": status,
    }


def get_reschedule_slot_options(db, advisor_id: str, booking_date: str, old_start: str) -> dict:
    """slot ว่างสำหรับหน้าเลื่อนคิว (ใช้ร่วมกันทั้งนักศึกษาและอาจารย์) — query ManageTimeSlots ครั้งเดียว

    - dates   : {date: [slot, ...]} ตั้งแต่ "พรุ่งนี้" เป็นต้นไป (โหมดเลื่อนวันและเวลา)
    - same_day: slot "ทั้งหมด" ที่อาจารย์ตั้งไว้ในวันเดิมของคิว พร้อม status (โหมดเลื่อนเฉพาะเวลา)
                แสดงช่วงที่เลือกไม่ได้ด้วย เพื่อให้ผู้ใช้เห็นว่าเต็ม/ปิด — เลือกได้เฉพาะ status="open"
    รวม slot จากทุก doc ของอาจารย์
    """
    min_date = get_tomorrow_str()
    dates: dict = {}
    same_day: list = []

    for doc in db["ManageTimeSlots"].find({"advisorId": advisor_id}, {"_id": 0, "dates": 1}):
        for date, slots in doc.get("dates", {}).items():
            if not isinstance(slots, list):
                continue
            if date == booking_date:
                same_day.extend(_same_day_slot(date, s, old_start) for s in slots)
            if date >= min_date:
                available_slots = _open_slots(slots)
                if available_slots:
                    dates.setdefault(date, []).extend(available_slots)

    return {"dates": dates, "same_day": sorted(same_day, key=lambda s: s["start"])}


def ensure_slot_open_for_reschedule(
    db, advisor_id: str, date: str, start: str, end: str, same_day: bool = False
) -> None:
    """ตรวจว่า slot ปลายทางที่จะเลื่อนคิวไปยังมีจริงและว่างอยู่ (raise HTTPException ถ้าไม่ผ่าน)

    วันที่ต้องเป็น YYYY-MM-DD และตั้งแต่พรุ่งนี้เป็นต้นไป (ตรงกับที่หน้าเลือกเวลาแสดง)
    same_day=True (เลื่อนเฉพาะเวลาในวันเดิม) อนุญาตวันนี้ได้ ถ้า slot ยังไม่ถึง cutoff
    ไม่พบ slot ของอาจารย์ในวันนั้น = 404 (เดิมปล่อยผ่านจนย้ายคิวไปวัน/เวลาที่ไม่มีอยู่จริงได้)
    """
    if same_day:
        # โหมดเลื่อนเฉพาะเวลา: ผู้เรียกตรวจแล้วว่าเป็นวันเดิมของคิว (อาจเป็นวันนี้) — ต้องยังไม่ถึง cutoff
        if not _DATE_RE.match(date or "") or is_within_cutoff(date, start):
            raise HTTPException(status_code=400, detail="ช่วงเวลานี้ใกล้ถึงเวลานัดเกินไป กรุณาเลือกช่วงเวลาอื่น")
    elif not _DATE_RE.match(date or "") or date < get_tomorrow_str():
        raise HTTPException(status_code=400, detail="วันที่ต้องเป็นรูปแบบ YYYY-MM-DD และตั้งแต่พรุ่งนี้เป็นต้นไป")

    _, _, target = _find_slot(db["ManageTimeSlots"], advisor_id, date, start, end)
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

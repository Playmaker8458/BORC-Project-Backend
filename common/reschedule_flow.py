"""ขั้นตอนที่ใช้ร่วมกันของการเลื่อนคิว ระหว่างฝั่งนักศึกษา (Reschedule_Students.py) และอาจารย์ (Reschedule_Advisor.py)

สองไฟล์นั้นเคยมีสำเนาของตรรกะตรวจ "โหมดการเลื่อน" และบล็อกเขียนประวัติหลังเลื่อนซ้ำกันทุกตัวอักษร
ต่างกันแค่ผู้กระทำ (role/id) กับฟิลด์สิทธิ์ที่ติดธง (RescheduledOnce / AdvisorRescheduledOnce)
ส่วนการแจ้งเตือน chatbot ยังอยู่ใน router แต่ละฝั่ง เพราะ URL/ payload ต่างกัน
"""

import logging

from fastapi import HTTPException

from common.parallel import run_parallel
from common.queue_history import log_queue_management_history
from common.reschedule_history import insert_reschedule_history

logger = logging.getLogger(__name__)

RESCHEDULE_MODES = ("date_time", "time_only")


def resolve_same_day_mode(mode: str, new_date: str, new_start: str, booking_date: str, old_start: str) -> bool:
    """ตรวจโหมดการเลื่อนแล้วคืนว่าเป็น "เลื่อนเฉพาะเวลาในวันเดิม" (same_day) หรือไม่; raise 400 ถ้าไม่ถูกต้อง"""
    if mode not in RESCHEDULE_MODES:
        raise HTTPException(status_code=400, detail="รูปแบบการเลื่อนคิวไม่ถูกต้อง")
    same_day = mode == "time_only"
    if same_day:
        if new_date != booking_date:
            raise HTTPException(status_code=400, detail="การเลื่อนเฉพาะเวลาต้องเป็นวันเดียวกับนัดเดิม")
        if new_start == old_start:
            raise HTTPException(status_code=400, detail="กรุณาเลือกช่วงเวลาที่ต่างจากเวลาเดิม")
    return same_day


def record_reschedule_history(
    db,
    *,
    booking: dict,
    actor_id: str,
    actor_role: str,
    advisor_id: str,
    advisor_name: str,
    student_id: str,
    student_name: str,
    new_date: str,
    new_start: str,
    new_end: str,
    new_label: str,
    reason: str,
    old_start: str,
    old_end: str,
    now,
    log_tag: str = "RescheduleBooking",
) -> None:
    """เขียนประวัติการเลื่อน 2 รายการพร้อมกัน

    คิวถูกเลื่อนไปแล้วใน DB ก่อนเรียกฟังก์ชันนี้ — ห้ามทำให้ request ล้ม ไม่งั้นจะตอบ 500
    ทั้งที่เลื่อนสำเร็จแล้ว จึงกลืน exception แล้วบันทึก log แทน
    """
    try:
        run_parallel(
            lambda: insert_reschedule_history(
                db,
                rescheduled_by_id=actor_id,
                rescheduled_by_role=actor_role,
                booking=booking,
                new_date=new_date,
                new_start=new_start,
                new_end=new_end,
                new_label=new_label,
                reason=reason,
                old_start=old_start,
                old_end=old_end,
                now=now,
            ),
            lambda: log_queue_management_history(
                db,
                advisor_id=advisor_id,
                advisor_name=advisor_name,
                student_id=student_id,
                student_name=student_name,
                status="Rescheduled",
                reason=reason,
                now=now,
            ),
        )
    except Exception:
        logger.exception("[%s] เลื่อนคิว %s แล้ว แต่เขียนประวัติไม่สำเร็จ", log_tag, booking["_id"])

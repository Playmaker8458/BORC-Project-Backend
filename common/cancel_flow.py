"""งานต่อจากการยกเลิกคิวที่ใช้ร่วมกันระหว่างฝั่งนักศึกษา (ManageQueueStudent.py) และอาจารย์ (ManageQueueAdvisor.py)

สองฝั่งเคยมีสำเนาของบล็อก "sync slot + เขียนประวัติ" เหมือนกัน ต่างกันแค่เอกสาร CancelBookingHistory
ที่แต่ละฝั่งประกอบเอง (ผู้ยกเลิก/ฟิลด์เสริมต่างกัน) ส่วนการแจ้งเตือน chatbot ยังอยู่ใน router แต่ละฝั่ง
"""

import logging

from common.parallel import run_parallel
from common.queue_history import log_queue_management_history
from common.slot_service import sync_slot_for_booking

logger = logging.getLogger(__name__)


def record_cancellation_effects(db, booking: dict, *, history_doc: dict, reason: str, now) -> None:
    """คืน slot + เขียน CancelBookingHistory + เขียนประวัติการจัดการคิว พร้อมกัน (3 งานไม่พึ่งกัน)

    คิวถูกยกเลิกแล้วใน DB ก่อนเรียกฟังก์ชันนี้ — ห้ามทำให้ request ล้ม เดิมถ้าตัวใด raise จะตอบ 500
    ทั้งที่ยกเลิกสำเร็จ และ background task แจ้งเตือน LINE ไม่ถูกรัน (FastAPI ไม่รัน background tasks
    เมื่อ response เป็น error) จึงกลืน exception แล้วบันทึก log แทน
    """
    try:
        run_parallel(
            lambda: sync_slot_for_booking(db, booking),
            lambda: db["CancelBookingHistory"].insert_one(history_doc),
            lambda: log_queue_management_history(
                db,
                advisor_id=booking.get("AdvisorId", ""),
                advisor_name=booking.get("Advisor_Name", ""),
                student_id=booking.get("UserId", ""),
                student_name=booking.get("StudentName", ""),
                status="Cancelled",
                reason=reason,
                now=now,
            ),
        )
    except Exception:
        logger.exception("[Cancel] ยกเลิกคิว %s แล้ว แต่ sync slot/เขียนประวัติไม่สำเร็จ", booking["_id"])

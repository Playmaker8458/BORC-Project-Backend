"""บันทึกประวัติการเลื่อนคิว (RescheduleHistory) — ใช้ร่วมกันโดย Reschedule_Advisor.py และ
Reschedule_Students.py ซึ่งเดิมมีสำเนาของ dict เดียวกันนี้แยกกัน ต่างกันแค่ค่า/ฟิลด์เล็กน้อย
(ผู้เลื่อน + role) ทำให้แก้ schema ที่จุดเดียวไม่ครบทุกที่ได้ง่าย
"""

from datetime import datetime


def insert_reschedule_history(
    db,
    *,
    rescheduled_by_id: str,
    rescheduled_by_role: str,
    booking: dict,
    new_date: str,
    new_start: str,
    new_end: str,
    new_label: str,
    reason: str,
    old_start: str,
    old_end: str,
    now: datetime,
) -> None:
    db["RescheduleHistory"].insert_one({
        "rescheduledById"  : rescheduled_by_id,
        "rescheduledByRole": rescheduled_by_role,
        "bookingId"        : str(booking["_id"]),
        "studentId"        : booking.get("UserId", ""),
        "advisorId"        : booking.get("AdvisorId", ""),
        "advisorName"      : booking.get("Advisor_Name", ""),
        "studentName"      : booking.get("StudentName", ""),
        "oldDate"          : booking.get("Date", ""),
        "oldStart"         : old_start,
        "oldEnd"           : old_end,
        "newDate"          : new_date,
        "newStart"         : new_start,
        "newEnd"           : new_end,
        "newLabel"         : new_label,
        "rescheduledReason": reason,
        "status"           : "Rescheduled",
        "createdAt"        : now,
        "updatedAt"        : now,
    })

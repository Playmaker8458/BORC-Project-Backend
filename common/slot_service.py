"""Logic กลางสำหรับ sync สถานะ slot (ManageTimeSlots) กับ booking จริง (BookingOnline)

เดิมรวมมาจาก users/router/Students/BookingOnline.py, Reschedule_Students.py,
Advisor/ManageQueueAdvisor.py, Advisor/Reschedule_Advisor.py ซึ่งแต่ละไฟล์เคย
มีสำเนาของ logic เดียวกันนี้แยกกัน ทำให้แก้บั๊กที่จุดเดียวไม่ครบทุกที่ได้ง่าย

ไฟล์นี้เคยยาว ~465 บรรทัดรวมทุกอย่างไว้ที่เดียว ตอนนี้แยกเป็น 3 ไฟล์ตามหน้าที่:
  - common/booking_cutoffs.py    กฎเรื่องเวลา cutoff + "slot จองได้หรือไม่"
  - common/slot_sync.py          เขียน DB sync booked/isLocked ของ slot
  - common/reschedule_options.py หา slot ว่างสำหรับเลื่อนคิว + ย้าย/ยกเลิก booking

โมดูลนี้ยังคง re-export ชื่อเดิมทั้งหมดไว้ เพราะมีหลาย router ที่ import จาก
`common.slot_service` โดยตรง (เช่น `from common.slot_service import recalculate_slot_booked`)
"""

from common.booking_cutoffs import (
    CUTOFF_HOURS,
    CUTOFF_HOURS_AFTER,
    CUTOFF_HOURS_BEFORE,
    can_cancel_approved,
    cutoff_datetime,
    get_now_utc7,
    get_today_str,
    get_tomorrow_str,
    has_bookable_slot,
    has_started,
    is_within_advisor_cutoff_window,
    is_within_cutoff,
    slot_is_taken,
    split_time_range,
    unavailable_advisor_ids,
    validate_date_or_400,
)
from common.reschedule_options import (
    ensure_slot_open_for_reschedule,
    get_reschedule_slot_options,
    mark_booking_cancelled,
    move_booking_to_slot,
)
from common.slot_sync import recalculate_slot_booked, sync_slot_for_booking, update_slot

__all__ = [
    "CUTOFF_HOURS",
    "CUTOFF_HOURS_AFTER",
    "CUTOFF_HOURS_BEFORE",
    "can_cancel_approved",
    "cutoff_datetime",
    "ensure_slot_open_for_reschedule",
    "get_now_utc7",
    "get_reschedule_slot_options",
    "get_today_str",
    "get_tomorrow_str",
    "has_bookable_slot",
    "has_started",
    "is_within_advisor_cutoff_window",
    "is_within_cutoff",
    "mark_booking_cancelled",
    "move_booking_to_slot",
    "recalculate_slot_booked",
    "slot_is_taken",
    "split_time_range",
    "sync_slot_for_booking",
    "unavailable_advisor_ids",
    "update_slot",
    "validate_date_or_400",
]

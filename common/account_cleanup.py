"""ล้างข้อมูลที่ผูกกับบัญชีที่ผู้ดูแลระบบลบ (เรียกจาก Admin/router/ManagementAccount.py: Delete_AccountUser)

นโยบาย: บัญชีที่ไม่ได้เป็นส่วนหนึ่งของระบบแล้ว → ลบข้อมูลของบัญชีนั้นออกให้หมด
ข้อยกเว้นเดียว: ประวัติการจัดการคิวที่อาจารย์เห็น (หน้า QueuehistoryAdvisor) ต้องเก็บไว้
ประวัติเหล่านั้นอยู่ใน 3 คอลเลกชัน (ApprovedHistory / RescheduleHistory / CancelBookingHistory) และใช้ชื่อนักศึกษาที่เก็บซ้ำในเอกสารเอง
จึง "ถอดการผูก" กับ id ของบัญชีที่ถูกลบ (แทนด้วย DETACHED_ID) แทนการลบ — ไม่งั้นบัญชีใหม่ที่ LINE userId เดิม
จะได้ประวัติ/สถิติ (BookingStats) ของบัญชีเก่ากลับมา เพราะ LINE userId เป็นคีย์เดิมเสมอ

เพิ่มคอลเลกชันใหม่ที่เก็บ id ผู้ใช้ → ต้องเพิ่มลง COLLECTION_POLICY ด้านล่าง (มีเทสต์ตรวจ: tests/test_account_cleanup.py)
"""

import logging

logger = logging.getLogger(__name__)

# ค่าแทน id ของบัญชีที่ถูกลบในประวัติที่เก็บไว้ — ไม่ตรงกับ LINE userId ของใคร จึงไม่มี query ของผู้ใช้คนไหนจับได้
DETACHED_ID = "deleted-account"

# นโยบายของทุกคอลเลกชันที่ระบบอ่าน/เขียน (ทดสอบว่าครบด้วยการสแกนซอร์ส)
#   "delete_account"  = ลบเมื่อบัญชีถูกลบ (ลบใน Delete_AccountUser หรือ purge_account_data)
#   "detach"          = เก็บไว้แต่ถอดการผูกกับบัญชี (ประวัติฝั่งอาจารย์)
#   "keep"            = ไม่ใช่ข้อมูลของผู้ใช้รายคน / เป็นบันทึกการกระทำของแอดมิน
COLLECTION_POLICY = {
    "UserProfile": "delete_account",
    "BookingOnline": "delete_account",
    "ManageQueueStudent": "delete_account",
    "ManageTimeSlots": "delete_account",
    "ConsultationAvailability": "delete_account",
    "ChatMessages": "delete_account",
    "ChatReadState": "delete_account",
    "NotificationReadState": "delete_account",
    "QueueManagementHistory": "delete_account",  # เฉพาะแถวของบัญชีนั้นเอง (userId = บัญชีที่ลบ)
    "ApprovedHistory": "detach",
    "RescheduleHistory": "detach",
    "CancelBookingHistory": "detach",
    "AccountManagementHistory": "keep",  # log การกระทำของแอดมิน (มีแค่ชื่อ/บทบาท)
    "LoginAdmin": "keep",  # บัญชีแอดมิน ไม่เกี่ยวกับบัญชีผู้ใช้
}


# ฟิลด์ประวัติที่เก็บ id ผู้ใช้ (คอลเลกชัน, ฟิลด์) แยกตาม role — ใช้ถอดการผูกตอนลบบัญชี
_DETACH_FIELDS = {
    "Student": [
        ("RescheduleHistory", "rescheduledById"),
        ("RescheduleHistory", "studentId"),
        ("CancelBookingHistory", "cancelledById"),
        ("ApprovedHistory", "UserId"),
    ],
    "Advisor": [
        ("RescheduleHistory", "rescheduledById"),
        ("RescheduleHistory", "advisorId"),
        ("CancelBookingHistory", "advisorId"),
        ("CancelBookingHistory", "cancelledById"),
        ("ApprovedHistory", "AdvisorId"),
    ],
}


def account_data_plan(user_id: str, role: str | None = None):
    """แผนล้างข้อมูลของบัญชี: (รายการที่ลบ, รายการที่ถอดการผูก)

    deletes = [(ชื่อผลลัพธ์, คอลเลกชัน, filter)]   detaches = [(คอลเลกชัน, ฟิลด์)]
    role=None คือไม่ทราบว่าบัญชีที่ไม่มีแล้วเคยเป็นอะไร → รวมเงื่อนไขของทั้งสอง role (ใช้กับสคริปต์ล้างข้อมูลเก่า)
    ทั้ง purge_account_data และ scripts/purge_orphaned_account_data.py ใช้แผนนี้ที่เดียว จึงไม่มีกฎซ้ำสองชุด
    """
    if role not in (None, "Student", "Advisor"):
        raise ValueError(f"ไม่รองรับ Role: {role}")

    if role == "Student":
        chat_filter = {"student_id": user_id}
    elif role == "Advisor":
        chat_filter = {"advisor_id": user_id}
    else:
        chat_filter = {"$or": [{"student_id": user_id}, {"advisor_id": user_id}]}

    deletes = [
        ("chat_messages", "ChatMessages", chat_filter),
        ("chat_read_state", "ChatReadState", {"$or": [{"readerId": user_id}, {"counterpartId": user_id}]}),
        ("notification_read_state", "NotificationReadState", {"userId": user_id}),
        # แถวของบัญชีนั้นเอง (มุมมองของเขา) ลบ; แถวของอีกฝ่ายที่มีชื่อบัญชีนี้อยู่ (userId เป็น id ของอีกฝ่าย) ไม่ถูกแตะ
        ("own_queue_history", "QueueManagementHistory", {"userId": user_id}),
    ]
    if role is None:
        detaches = list(dict.fromkeys(_DETACH_FIELDS["Student"] + _DETACH_FIELDS["Advisor"]))
    else:
        detaches = list(_DETACH_FIELDS[role])
    return deletes, detaches


def purge_account_data(db, user_id: str, role: str | None = None, *, dry_run: bool = False) -> dict:
    """ลบ/ถอดการผูกข้อมูลของบัญชี `user_id` (LINE userId) คืนจำนวนที่ลบ/แก้ ไว้แสดงผลและทดสอบ

    dry_run=True นับอย่างเดียว ไม่เขียนอะไร (ตัวเลขเท่ากับที่จะได้ตอนทำจริง)
    ทำซ้ำได้ปลอดภัย (ไม่มีข้อมูลเหลือให้ลบ = 0) จึงเรียกก่อนลบโปรไฟล์ได้ ถ้าล้มกลางคันแอดมินกดลบซ้ำได้
    """
    deletes, detaches = account_data_plan(user_id, role)

    counts = {}
    for key, collection, filt in deletes:
        counts[key] = db[collection].count_documents(filt) if dry_run else db[collection].delete_many(filt).deleted_count

    detached = 0
    for collection, field in detaches:
        if dry_run:
            detached += db[collection].count_documents({field: user_id})
        else:
            detached += db[collection].update_many({field: user_id}, {"$set": {field: DETACHED_ID}}).modified_count
    counts["detached_history"] = detached

    if not dry_run:
        logger.info("ล้างข้อมูลบัญชี %s (%s): %s", user_id, role or "ไม่ทราบ role", counts)
    return counts

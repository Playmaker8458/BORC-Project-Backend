"""บันทึกประวัติการจัดการคิวให้ทั้งฝั่งอาจารย์และนักศึกษาเห็นเหตุการณ์เดียวกัน

รวมมาจาก pattern insert_many([...]) สองระเบียน (Advisor + Student) ที่เคยเขียน
ซ้ำกันในทุก endpoint ที่เปลี่ยนสถานะคิว (อนุมัติ/ยกเลิก/เลื่อน/ปิดการให้คำปรึกษา)
"""

from datetime import datetime


def log_queue_management_history(
    db,
    *,
    advisor_id: str,
    advisor_name: str,
    student_id: str,
    student_name: str,
    status: str,
    reason: str | None,
    now: datetime,
) -> None:
    db["QueueManagementHistory"].insert_many([
        {
            "userId"    : advisor_id,
            "role"      : "Advisor",
            "UserName"  : student_name,
            "status"    : status,
            "Reason"    : reason,
            "createdAt" : now,
            "updatedAt" : now,
        },
        {
            "userId"    : student_id,
            "role"      : "Student",
            "UserName"  : advisor_name,
            "status"    : status,
            "Reason"    : reason,
            "createdAt" : now,
            "updatedAt" : now,
        },
    ])

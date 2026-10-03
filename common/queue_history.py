"""บันทึกประวัติการจัดการคิวให้ทั้งฝั่งอาจารย์และนักศึกษาเห็นเหตุการณ์เดียวกัน

รวมมาจาก pattern insert_many([...]) สองระเบียน (Advisor + Student) ที่เคยเขียน
ซ้ำกันในทุก endpoint ที่เปลี่ยนสถานะคิว (อนุมัติ/ยกเลิก/เลื่อน/ปิดการให้คำปรึกษา)
"""

from datetime import datetime

RECIPIENT_CHOICES = {"both", "advisor", "student"}


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
    recipients: str = "both",
) -> None:
    """recipients: "both" (ค่าเริ่มต้น) = อาจารย์และนักศึกษาเห็นเหตุการณ์, "advisor"/"student" = เห็นเฉพาะฝ่ายนั้น

    แถวนี้คือแจ้งเตือนในแอป (กระดิ่ง) ของเจ้าของ userId — เหตุการณ์ที่ฝ่ายหนึ่งทำเอง (อนุมัติ/เลื่อนคิว) แจ้งเฉพาะ
    ฝั่งตรงข้าม; ยกเลิก/จองคิวใหม่ (Pending)/ปิดการให้คำปรึกษาแจ้งทั้งสองฝ่าย รายการของแต่ละฝ่ายแสดงชื่ออีกฝ่าย
    """
    if recipients not in RECIPIENT_CHOICES:
        raise ValueError(f"recipients must be one of {sorted(RECIPIENT_CHOICES)}, got {recipients!r}")
    rows = []
    if recipients in ("both", "advisor"):
        rows.append({
            "userId"    : advisor_id,
            "role"      : "Advisor",
            "UserName"  : student_name,
            "status"    : status,
            "Reason"    : reason,
            "createdAt" : now,
            "updatedAt" : now,
        })
    if recipients in ("both", "student"):
        rows.append({
            "userId"    : student_id,
            "role"      : "Student",
            "UserName"  : advisor_name,
            "status"    : status,
            "Reason"    : reason,
            "createdAt" : now,
            "updatedAt" : now,
        })
    db["QueueManagementHistory"].insert_many(rows)

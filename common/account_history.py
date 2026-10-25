"""ตารางประวัติการจัดการบัญชี (AccountManagementHistory): ที่เดียวที่กำหนดป้ายสถานะและวิธีเขียน

ใครเขียนอะไร:
- หน้า "ยืนยันการเข้าสู่ระบบ"  (Admin/router/LoginVerification.py)   → VERIFIED
- หน้า "จัดการบัญชี" ผู้ใช้งาน  (Admin/router/ManagementAccount.py) → EDITED, DELETED
- หน้า "จัดการบัญชี" ผู้ดูแลระบบ (Admin/router/AddAdmin.py + ManagementAccount.py ส่วนผู้ดูแลระบบ) → ADMIN_CREATED, EDITED, DELETED (kind="admin")
หน้า "ประวัติจัดการบัญชี" อ่านตารางนี้ แยกแท็บผู้ใช้งาน/ผู้ดูแลระบบตาม kind
ป้ายสถานะต้องตรงกับตารางสีใน frontend (routes/admin/account-history/historyShared.ts)
"""

from datetime import datetime, timezone
from typing import Optional

VERIFIED = "ยืนยันสิทธิ์แล้ว"
EDITED = "แก้ไขบัญชีแล้ว"
DELETED = "ลบบัญชีแล้ว"
ADMIN_CREATED = "สร้างบัญชีแล้ว"


def record_user_history(col_history, prefix: str, first_name: str, last_name: str, role: str, status_label: str) -> None:
    """เหตุการณ์ของบัญชีผู้ใช้งาน (นักศึกษา/อาจารย์); role = ค่าจริงจากฐานข้อมูล (Student / Advisor)"""
    col_history.insert_one({
        "Prefix":      prefix,
        "firstName":   first_name,
        "lastName":    last_name,
        "role":        role,
        "statusLabel": status_label,
        "createdAt":   datetime.now(timezone.utc),
    })


def record_admin_history(col_history, status_label: str, target_email: str, actor_email: str,
                         permissions: Optional[list[str]] = None, level: Optional[str] = None) -> None:
    """เหตุการณ์ของบัญชีผู้ดูแลระบบ (kind="admin"): firstName = อีเมลของบัญชีที่ถูกกระทำ
    backend ส่งรายการเหล่านี้ให้เฉพาะผู้มีสิทธิ์ "เพิ่มบัญชีผู้ดูแลระบบ" (ดู Admin/router/AccountManagementHistory.py)"""
    col_history.insert_one({
        "kind":        "admin",
        "Prefix":      "",
        "firstName":   target_email,
        "lastName":    "",
        "role":        "Admin",
        "statusLabel": status_label,
        "actorEmail":  actor_email,
        "permissions": permissions,
        "level":       level,
        "createdAt":   datetime.now(timezone.utc),
    })

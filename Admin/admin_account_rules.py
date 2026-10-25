"""กฎร่วมของบัญชีผู้ดูแลระบบ ที่ใช้โดย 2 หน้า:
- หน้า "เพิ่มบัญชีผู้ดูแลระบบ"                 → Admin/router/AddAdmin.py
- หน้า "จัดการบัญชี" ส่วนผู้ดูแลระบบ (แก้สิทธิ์/ลบ) → Admin/router/ManagementAccount.py
ไฟล์นี้ไม่มี endpoint เอง"""

import logging
from typing import Optional

from fastapi import HTTPException

from Admin.Database.ConnectDB import Connect_MongoDB
from Admin.auth.authAdmin import ADMIN_PERMISSIONS
from common.account_history import record_admin_history

logger = logging.getLogger(__name__)

# ป้ายระดับของบัญชี (เป็นข้อมูลไว้ดูเท่านั้น ไม่มีผลต่อสิทธิ์ — สิทธิ์จริงตัดสินที่ Permissions)
ADMIN_LEVELS = ("mid", "low")


def admin_col():
    return Connect_MongoDB()["BORC"]["LoginAdmin"]


def log_admin_event(label: str, target_email: str, actor_email: str,
                    permissions: Optional[list[str]] = None, level: Optional[str] = None) -> None:
    """บันทึกประวัติการสร้าง/แก้สิทธิ์/ลบบัญชีผู้ดูแลระบบ (รูปแบบเรคคอร์ดและป้ายสถานะอยู่ที่ common/account_history.py)
    บันทึกไม่สำเร็จไม่ทำให้การกระทำหลักล้มเหลว (การสร้าง/แก้/ลบสำเร็จไปแล้ว) แต่จะมี log ให้ตามดู"""
    try:
        record_admin_history(Connect_MongoDB()["BORC"]["AccountManagementHistory"], label, target_email, actor_email, permissions, level)
    except Exception:
        logger.exception("บันทึกประวัติบัญชีผู้ดูแลระบบไม่สำเร็จ")


def clean_permissions(values: list[str]) -> list[str]:
    unknown = [p for p in values if p not in ADMIN_PERMISSIONS]
    if unknown:
        raise HTTPException(status_code=400, detail="พบสิทธิ์ที่ไม่ถูกต้อง")
    permissions = [p for p in ADMIN_PERMISSIONS if p in values]  # ตัดซ้ำ + เรียงตามมาตรฐาน
    if not permissions:
        raise HTTPException(status_code=400, detail="ต้องเลือกสิทธิ์อย่างน้อย 1 รายการ")
    return permissions


def validated_level(value: Optional[str]) -> Optional[str]:
    if value is not None and value not in ADMIN_LEVELS:
        raise HTTPException(status_code=400, detail="ระดับผู้ดูแลระบบไม่ถูกต้อง")
    return value

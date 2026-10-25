"""หน้า "เพิ่มบัญชีผู้ดูแลระบบ" (สิทธิ์ add): สร้างบัญชีผู้ดูแลระบบใหม่เท่านั้น
การดู/แก้สิทธิ์/ลบบัญชีผู้ดูแลระบบเป็นของหน้า "จัดการบัญชี" (Admin/router/ManagementAccount.py)"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from Admin.admin_account_rules import admin_col, clean_permissions, log_admin_event, validated_level
from Admin.auth.authAdmin import verify_token
from common.account_history import ADMIN_CREATED

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # ขีดจำกัดของ bcrypt 5.x
# อีเมลบัญชีใหม่รับเฉพาะอักขระ ASCII ปกติ: เคยมีบัญชีที่พิมพ์ตอนคีย์บอร์ดเป็นภาษาไทย ทำให้มีสระ/อักขระไทยติดหน้าอีเมลโดยมองไม่เห็น
# แล้วล็อกอินไม่ได้โดยไม่รู้สาเหตุ (การล็อกอินไม่ใช้กฎนี้ เพื่อให้บัญชีเดิมที่เป็นแบบนั้นยังเข้าและลบได้)
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")

router = APIRouter()


class CreateAdmin(BaseModel):
    email           : str
    password        : str
    confirm_password: str
    permissions     : list[str]
    level           : Optional[str] = None


def _validated_email(raw: str) -> str:
    email = raw.strip()
    if not email.isascii():
        raise HTTPException(status_code=400, detail="อีเมลต้องเป็นตัวอักษรภาษาอังกฤษ ตัวเลข และสัญลักษณ์ปกติเท่านั้น (ตรวจว่าคีย์บอร์ดไม่ได้เป็นภาษาไทย)")
    if not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=400, detail="รูปแบบอีเมลไม่ถูกต้อง")
    return email


def _validated_password_bytes(password: str, confirm_password: str) -> bytes:
    """ตรวจรหัสผ่านใหม่ตามลำดับ: ตรงกัน → ยาวพอ → ไม่เกินขีดจำกัดของ bcrypt แล้วคืนค่าเป็นไบต์ที่พร้อม hash"""
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="รหัสผ่านไม่ตรงกัน")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"รหัสผ่านต้องมีอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร")
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=400, detail=f"รหัสผ่านต้องไม่เกิน {MAX_PASSWORD_BYTES} ไบต์ (ตัวอักษรไทยนับ 3 ไบต์ต่อตัว)")
    return password_bytes


def _email_already_used(col, email: str) -> bool:
    # เทียบอีเมลแบบไม่สนตัวพิมพ์เล็ก-ใหญ่ (คอลเลกชันนี้ไม่มี unique index)
    return col.find_one({"Email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}}) is not None


# ใช้ได้เฉพาะแอดมินที่มีสิทธิ์ "add" — ผูกสิทธิ์ที่ main.py ตอน include_router
@router.post("/AddAdminAccount", status_code=status.HTTP_201_CREATED)
def create_admin(data: CreateAdmin, payload: dict = Depends(verify_token)):
    email = _validated_email(data.email)
    password_bytes = _validated_password_bytes(data.password, data.confirm_password)
    permissions = clean_permissions(data.permissions)
    level = validated_level(data.level)

    try:
        col = admin_col()
        if _email_already_used(col, email):
            raise HTTPException(status_code=409, detail="อีเมลนี้ถูกใช้เป็นบัญชีผู้ดูแลระบบแล้ว")

        now = datetime.now(timezone.utc)
        col.insert_one({
            "Email"      : email,  # เก็บตามที่พิมพ์: Login เทียบอีเมลแบบตรงตัว
            "Password"   : bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8"),
            "Permissions": permissions,
            **({"Level": level} if level is not None else {}),
            "createdBy"  : payload["email"],  # ใช้แยกบัญชีแรกสุด (ไม่มีค่านี้) ออกจากบัญชีที่สร้างผ่านหน้าเว็บ
            "createdAt"  : now,
            "updatedAt"  : now,
        })
        log_admin_event(ADMIN_CREATED, email, payload["email"], permissions, level)
    except HTTPException:
        raise
    except Exception:
        logger.exception("สร้างบัญชีผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"message": "สร้างบัญชีผู้ดูแลระบบสำเร็จ", "email": email, "permissions": permissions}

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from Admin.Database.ConnectDB import Connect_MongoDB
from Admin.auth.authAdmin import ADMIN_PERMISSIONS, get_admin_permissions, verify_token

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # ขีดจำกัดของ bcrypt 5.x
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# ป้ายระดับของบัญชี (เป็นข้อมูลไว้ดูเท่านั้น ไม่มีผลต่อสิทธิ์ — สิทธิ์จริงตัดสินที่ Permissions)
ADMIN_LEVELS = ("mid", "low")

router = APIRouter()


class CreateAdmin(BaseModel):
    email           : str
    password        : str
    confirm_password: str
    permissions     : list[str]
    level           : Optional[str] = None


class UpdateAdminPermissions(BaseModel):
    permissions: list[str]
    level      : Optional[str] = None


def _admin_col():
    return Connect_MongoDB()["BORC"]["LoginAdmin"]


def _clean_permissions(values: list[str]) -> list[str]:
    unknown = [p for p in values if p not in ADMIN_PERMISSIONS]
    if unknown:
        raise HTTPException(status_code=400, detail="พบสิทธิ์ที่ไม่ถูกต้อง")
    permissions = [p for p in ADMIN_PERMISSIONS if p in values]  # ตัดซ้ำ + เรียงตามมาตรฐาน
    if not permissions:
        raise HTTPException(status_code=400, detail="ต้องเลือกสิทธิ์อย่างน้อย 1 รายการ")
    return permissions


def _validated_level(value: Optional[str]) -> Optional[str]:
    if value is not None and value not in ADMIN_LEVELS:
        raise HTTPException(status_code=400, detail="ระดับผู้ดูแลระบบไม่ถูกต้อง")
    return value


def _validated_email(raw: str) -> str:
    email = raw.strip()
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


def _is_protected(admin: dict) -> bool:
    """บัญชีแรกสุดของระบบ (seed) ไม่มี createdBy — ห้ามลบ/ลดสิทธิ์ผ่านแอป (เปลี่ยนใน DB เองเท่านั้น)"""
    return admin.get("createdBy") is None


def _is_full(admin: dict) -> bool:
    return len(get_admin_permissions(admin)) == len(ADMIN_PERMISSIONS)


def _other_full_admin_exists(col, admin_id: ObjectId) -> bool:
    # แอดมินเดิมที่ไม่มีฟิลด์ Permissions นับเป็นสิทธิ์เต็ม (ตรงกับ get_admin_permissions)
    return col.find_one({
        "_id": {"$ne": admin_id},
        "$or": [
            {"Permissions": {"$exists": False}},
            {"Permissions": None},
            {"Permissions": {"$all": list(ADMIN_PERMISSIONS)}},
        ],
    }) is not None


def _load_target(col, admin_id: str, payload: dict) -> dict:
    """โหลดบัญชีเป้าหมาย + กฎร่วมของการลบ/แก้สิทธิ์: ไม่ใช่ตัวเอง และไม่ใช่บัญชีแรกสุด"""
    try:
        oid = ObjectId(admin_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="ไม่พบบัญชีผู้ดูแลระบบนี้")
    admin = col.find_one({"_id": oid})
    if admin is None:
        raise HTTPException(status_code=404, detail="ไม่พบบัญชีผู้ดูแลระบบนี้")
    if str(oid) == payload["user_id"]:
        raise HTTPException(status_code=403, detail="ไม่สามารถลบหรือแก้สิทธิ์ของบัญชีตัวเองได้")
    if _is_protected(admin):
        raise HTTPException(status_code=403, detail="บัญชีผู้ดูแลระบบแรกสุดของระบบ ลบหรือลดสิทธิ์ผ่านหน้านี้ไม่ได้")
    return admin


# ใช้ได้เฉพาะแอดมินที่มีสิทธิ์ "add" — ผูกสิทธิ์ที่ main.py ตอน include_router
@router.get("")
def list_admins(payload: dict = Depends(verify_token)):
    try:
        docs = list(_admin_col().find({}).sort("_id", 1))
    except Exception:
        logger.exception("โหลดรายการผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")
    return [
        {
            "_id"        : str(d["_id"]),
            "Email"      : d.get("Email", ""),
            "Permissions": get_admin_permissions(d),
            "Level"      : d.get("Level"),
            "createdAt"  : d.get("createdAt"),
            "createdBy"  : d.get("createdBy"),
            "isProtected": _is_protected(d),
            "isSelf"     : str(d["_id"]) == payload["user_id"],
        }
        for d in docs
    ]


@router.patch("/{admin_id}")
def update_admin_permissions(admin_id: str, data: UpdateAdminPermissions, payload: dict = Depends(verify_token)):
    permissions = _clean_permissions(data.permissions)
    level = _validated_level(data.level)
    try:
        col = _admin_col()
        admin = _load_target(col, admin_id, payload)
        if _is_full(admin) and len(permissions) < len(ADMIN_PERMISSIONS) and not _other_full_admin_exists(col, admin["_id"]):
            raise HTTPException(status_code=409, detail="ต้องเหลือผู้ดูแลระบบที่มีสิทธิ์เต็มอย่างน้อย 1 คน")
        changes = {"Permissions": permissions, "updatedAt": datetime.now(timezone.utc)}
        if level is not None:  # ไม่ส่งระดับมา = คงป้ายเดิมไว้
            changes["Level"] = level
        col.update_one({"_id": admin["_id"]}, {"$set": changes})
    except HTTPException:
        raise
    except Exception:
        logger.exception("แก้สิทธิ์ผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"message": "แก้สิทธิ์ผู้ดูแลระบบสำเร็จ", "permissions": permissions}


@router.delete("/{admin_id}")
def delete_admin(admin_id: str, payload: dict = Depends(verify_token)):
    try:
        col = _admin_col()
        admin = _load_target(col, admin_id, payload)
        if _is_full(admin) and not _other_full_admin_exists(col, admin["_id"]):
            raise HTTPException(status_code=409, detail="ต้องเหลือผู้ดูแลระบบที่มีสิทธิ์เต็มอย่างน้อย 1 คน")
        col.delete_one({"_id": admin["_id"]})  # session ของบัญชีนี้ใช้ไม่ได้ทันที: verify_token ตรวจ DB ทุกคำขอ
    except HTTPException:
        raise
    except Exception:
        logger.exception("ลบบัญชีผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"message": "ลบบัญชีผู้ดูแลระบบสำเร็จ"}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_admin(data: CreateAdmin, payload: dict = Depends(verify_token)):
    email = _validated_email(data.email)
    password_bytes = _validated_password_bytes(data.password, data.confirm_password)
    permissions = _clean_permissions(data.permissions)
    level = _validated_level(data.level)

    try:
        col = _admin_col()
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
    except HTTPException:
        raise
    except Exception:
        logger.exception("สร้างบัญชีผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"message": "สร้างบัญชีผู้ดูแลระบบสำเร็จ", "email": email, "permissions": permissions}

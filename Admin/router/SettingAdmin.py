import logging

logger = logging.getLogger(__name__)
import bcrypt
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel
from Admin.Database.ConnectDB import Connect_MongoDB
from datetime import datetime, timezone
from bson import ObjectId
from Admin.auth.authAdmin import verify_token   # ✅ import verify จาก authAdmin

router = APIRouter()


# ─── Models ───────────────────────────────────────────────────────────────────
class ChangePassword(BaseModel):
    current_password : str
    new_password     : str
    confirm_password : str


# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_AdminByID(user_id: str) -> dict | None:
    try:
        db  = Connect_MongoDB()["BORC"]
        col = db["LoginAdmin"]
        return col.find_one({"_id": ObjectId(user_id)}, {"_id": 0, "Password": 1})
    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


def update_AdminPasswordDB(user_id: str, hashed_password: str) -> bool:
    try:
        db  = Connect_MongoDB()["BORC"]
        col = db["LoginAdmin"]
        result = col.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$set": {
                    "Password"  : hashed_password,
                    "updatedAt" : datetime.now(timezone.utc)
                }
            }
        )
        return result.modified_count > 0
    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในการเปลี่ยนรหัสผ่าน: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Routes ───────────────────────────────────────────────────────────────────
# ✅ PATCH /ChangePassword
@router.patch("/ChangePassword")
def change_password(
    data    : ChangePassword,
    payload : dict = Depends(verify_token)
):
    # ตรวจสอบรหัสผ่านใหม่ตรงกันไหม
    if data.new_password != data.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="รหัสผ่านใหม่ไม่ตรงกัน"
        )

    # ตรวจสอบความยาวรหัสผ่านใหม่
    if len(data.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="รหัสผ่านต้องมีอย่างน้อย 8 ตัวอักษร"
        )

    # ดึงรหัสผ่านปัจจุบันจาก DB
    admin = get_AdminByID(payload["user_id"])
    if not admin:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ดูแลระบบ")

    # ตรวจสอบรหัสผ่านปัจจุบัน
    if not bcrypt.checkpw(
        data.current_password.encode("utf-8"),
        admin["Password"].encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="รหัสผ่านปัจจุบันไม่ถูกต้อง"
        )

    # เข้ารหัสรหัสผ่านใหม่
    hashed = bcrypt.hashpw(
        data.new_password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    # บันทึกลง DB
    updated = update_AdminPasswordDB(payload["user_id"], hashed)
    if not updated:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ดูแลระบบ")

    return {"message": "เปลี่ยนรหัสผ่านสำเร็จ"}
import logging
import re

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime, timezone
from Admin.Database.ConnectDB import Connect_MongoDB
from common.user_cache import invalidate_user_cache
from dotenv import load_dotenv
from typing import Literal


router = APIRouter()

load_dotenv(override=True)

# ─────────────────────────────────────────
#  Schemas
# ─────────────────────────────────────────

class UpdateDataUser(BaseModel):
    Role: Literal["Student", "Advisor"]
    Status: Literal["Approved", "Pending", "Suspended"]

# ─────────────────────────────────────────
#  Helper: บันทึก History
# ─────────────────────────────────────────

def save_history(col_history, first_name: str, last_name: str, role: str, status_label: str):
    """
    บันทึกลง AccountManagementHistory ตามโครงสร้างที่ใช้แสดงในหน้าประวัติ:
      - ชื่อจริง   → firstName
      - นามสกุล   → lastName
      - ประเภท/สิทธิ์ → role
      - สถานะ     → statusLabel  (ยืนยันสิทธิ์แล้ว / ลบบัญชีแล้ว / แก้ไขบัญชีแล้ว)
    """
    record = {
        "firstName":   first_name,
        "lastName":    last_name,
        "role":        role,
        "statusLabel": status_label,          # ข้อความที่แสดงบน badge
        "createdAt":   datetime.now(timezone.utc),
    }
    col_history.insert_one(record)


# ─────────────────────────────────────────
#  GET  /Profile  – ดึงรายชื่อผู้รอยืนยันสิทธิ์ ทีละหน้า
# ─────────────────────────────────────────
# ผู้รอยืนยันสิทธิ์ = ไม่ถูกระงับ และไม่ใช่ "Approved ที่มีบทบาทแล้ว" (ตรงกับ isAwaitingVerification ฝั่ง frontend)
_AWAITING_VERIFICATION = {"$and": [
    {"Status": {"$ne": "Suspended"}},
    {"$nor": [{"Status": "Approved", "Role": {"$in": ["Student", "Advisor"]}}]},
]}


@router.get("/Profile")
def get_all_profiles(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str = Query("", max_length=100),
):
    """คืน {items, total, page, limit}: กรองสถานะ ค้นหาชื่อ/นามสกุล และแบ่งหน้าที่ MongoDB
    page เกินหน้าสุดท้ายจะถูกปรับกลับมาหน้าสุดท้าย (เช่น อนุมัติรายการสุดท้ายของหน้าไปแล้ว)"""
    col = Connect_MongoDB()["BORC"]["UserProfile"]
    query: dict = {**_AWAITING_VERIFICATION}
    term = search.strip()
    if term:
        rx = {"$regex": re.escape(term), "$options": "i"}
        query = {"$and": [*_AWAITING_VERIFICATION["$and"], {"$or": [{"Firstname": rx}, {"Lastname": rx}]}]}

    total = col.count_documents(query)
    page = min(page, max(1, -(-total // limit)))
    items = list(col.find(query, {"_id": 0}).sort("_id", 1).skip((page - 1) * limit).limit(limit))
    return {"items": items, "total": total, "page": page, "limit": limit}


# ─────────────────────────────────────────
#  PATCH  /Profile/{user_id}  – ยืนยัน / แก้ไขสิทธิ์
# ─────────────────────────────────────────
@router.patch("/Profile/{user_id}")
def update_role_user(user_id: str, update: UpdateDataUser):
    client = Connect_MongoDB()
    try:
        db          = client["BORC"]
        col         = db["UserProfile"]
        col_history = db["AccountManagementHistory"]

        user = col.find_one({"userId": user_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"ไม่พบผู้ใช้งาน userId={user_id}")

        status_label_map = {
            "Approved": "ยืนยันสิทธิ์แล้ว",
            "Edited":   "แก้ไขบัญชีแล้ว",
        }
        status_label = status_label_map.get(update.Status, "แก้ไขบัญชีแล้ว")

        col.update_one(
            {"userId": user_id},
            {"$set": {
                "Role":   update.Role,
                "Status": update.Status,
            }}
        )
        invalidate_user_cache(user_id)  # สิทธิ์/สถานะใหม่ต้องมีผลทันที ไม่รอ cache หมดอายุ
            
        save_history(
            col_history,
            first_name=user.get("Firstname", ""),
            last_name=user.get("Lastname",  ""),
            # ต้องใช้ update.Role (ค่าที่เพิ่งเขียนลง DB ข้างบน) ไม่ใช่ user.get("Role", ...)
            # เดิม user คือ document ที่ดึงมา "ก่อน" อัปเดต ซึ่งตอนบัญชียังเป็น Pending
            # จะมี Role="" (key มีอยู่จริง ไม่ใช่ None) ทำให้ .get(..., default) ไม่เคย
            # fallback มาใช้ default เลย — history บันทึก role ว่างเปล่าทุกครั้งที่ Approve
            role=update.Role,
            status_label=status_label,
        )

        return {
            "message":     "อัพเดตข้อมูลสำเร็จ",
            "userId":      user_id,
            "Role":        update.Role,
            "Status":      update.Status,
            "statusLabel": status_label,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

# ─────────────────────────────────────────
#  DELETE  /Profile/{user_id}  – ลบบัญชี
# ─────────────────────────────────────────
@router.delete("/Profile/{user_id}")
def delete_user(user_id: str):
    """
    - ลบ document ใน UserProfile
    - บันทึก history ด้วย statusLabel: "ลบบัญชีแล้ว"
    """
    client = Connect_MongoDB()
    try:
        db          = client["BORC"]
        col         = db["UserProfile"]
        col_history = db["AccountManagementHistory"]

        # ── ดึงข้อมูลก่อนลบ ───────────────────────────────────────────────
        user = col.find_one({"userId": user_id}, {"_id": 0})
        if not user:
            raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้งานนี้ในระบบ")

        # ── ลบจาก UserProfile ─────────────────────────────────────────────
        col.delete_one({"userId": user_id})
        invalidate_user_cache(user_id)

        # ── บันทึก History ────────────────────────────────────────────────
        save_history(
            col_history,
            first_name=user.get("Firstname", ""),
            last_name=user.get("Lastname",  ""),
            role=user.get("Role", ""),
            status_label="ลบบัญชีแล้ว",
        )

        return {"message": "ลบบัญชีสำเร็จ", "userId": user_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

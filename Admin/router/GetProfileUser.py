import logging
import re

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime, timezone
from Admin.Database.ConnectDB import Connect_MongoDB
from Admin.auth.authAdmin import verify_token
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

def save_history(col_history, Prefix: str, first_name: str, last_name: str, role: str, status_label: str):
    # record กำหนดข้อมูลสำหรับบันทึกลงในตาราง AccountManagementHistory 
    record = {
        "Prefix": Prefix,
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


# สิทธิ์ "ยืนยันการเข้าสู่ระบบ" (verify) อนุมัติได้เฉพาะบัญชีที่รอยืนยัน; การระงับ / เปลี่ยนสิทธิ์บัญชีที่อนุมัติแล้ว / ลบบัญชี
# ต้องมีสิทธิ์ "จัดการบัญชี" (manage) ด้วย — ไม่งั้นแอดมินระดับล่างข้ามขอบเขตสิทธิ์ผ่าน endpoint นี้ได้
NEEDS_MANAGE_DETAIL = "ต้องมีสิทธิ์จัดการบัญชี จึงจะแก้ไขหรือลบบัญชีที่ไม่ได้อยู่ในสถานะรอยืนยันได้"


def _can_manage(payload: dict) -> bool:
    return "manage" in payload["permissions"]


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
def update_role_user(user_id: str, update: UpdateDataUser, payload: dict = Depends(verify_token)):
    client = Connect_MongoDB()
    try:
        db          = client["BORC"]
        col         = db["UserProfile"]
        col_history = db["AccountManagementHistory"]

        user = col.find_one({"userId": user_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"ไม่พบผู้ใช้งาน userId={user_id}")

        can_manage = _can_manage(payload)
        if not can_manage and update.Status != "Approved":
            raise HTTPException(status_code=403, detail=NEEDS_MANAGE_DETAIL)

        status_label_map = {
            "Approved": "ยืนยันสิทธิ์แล้ว",
            "Edited":   "แก้ไขบัญชีแล้ว",
        }
        status_label = status_label_map.get(update.Status, "แก้ไขบัญชีแล้ว")

        target = {"userId": user_id} if can_manage else {"$and": [{"userId": user_id}, *_AWAITING_VERIFICATION["$and"]]}
        result = col.update_one(
            target,
            {"$set": {
                "Role":   update.Role,
                "Status": update.Status,
            }}
        )
        if result.matched_count == 0 and not can_manage:
            raise HTTPException(status_code=403, detail=NEEDS_MANAGE_DETAIL)
        invalidate_user_cache(user_id)  # สิทธิ์/สถานะใหม่ต้องมีผลทันที ไม่รอ cache หมดอายุ
            
        save_history(
            col_history,
            Prefix=user.get("Prefix", ""),
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
def delete_user(user_id: str, payload: dict = Depends(verify_token)):
    """
    - ลบ document ใน UserProfile
    - บันทึก history ด้วย statusLabel: "ลบบัญชีแล้ว"
    ต้องมีสิทธิ์ manage (หน้าเว็บไม่ได้เรียก endpoint นี้ — การลบบัญชีใช้ /ManagementAccount/DeleteAccountUser)
    """
    if not _can_manage(payload):
        raise HTTPException(status_code=403, detail=NEEDS_MANAGE_DETAIL)

    client = Connect_MongoDB()
    try:
        db          = client["BORC"]
        col         = db["UserProfile"]
        col_history = db["AccountManagementHistory"]

        # ── ดึงข้อมูลก่อนลบ ───────────────────────────────────────────────
        user = col.find_one({"userId": user_id})
        if not user:
            raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้งานนี้ในระบบ")

        

        # ── บันทึก History ────────────────────────────────────────────────
        save_history(
            col_history,
            Prefix=user.get("Prefix", ""),
            first_name=user.get("Firstname", ""),
            last_name=user.get("Lastname",  ""),
            role=user.get("Role", ""),
            status_label="ลบบัญชีแล้ว",
        )

        # ── ลบจาก UserProfile ─────────────────────────────────────────────
        col.delete_one({"userId": user_id})
        invalidate_user_cache(user_id)

        return {"message": "ลบบัญชีสำเร็จ", "userId": user_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
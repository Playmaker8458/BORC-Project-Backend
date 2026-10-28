"""หน้า "ยืนยันการเข้าสู่ระบบ" (สิทธิ์ verify): รายชื่อผู้รอยืนยัน และการอนุมัติเท่านั้น

ขอบเขตของไฟล์นี้: บัญชีที่ "ยังรอยืนยัน" → อนุมัติ (กำหนดบทบาท Student/Advisor)
หลังอนุมัติแล้ว การแก้ไข/ระงับ/ลบบัญชีเป็นของหน้า "จัดการบัญชี" (Admin/router/ManagementAccount.py, สิทธิ์ manage) ไม่ใช่ไฟล์นี้
"""

import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from Admin.Database.ConnectDB import Connect_MongoDB

from common.account_history import VERIFIED, record_user_history
from common.user_cache import invalidate_user_cache
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot

logger = logging.getLogger(__name__)

router = APIRouter()

chatbot_uri = CHATBOT_URL


class ApproveUser(BaseModel):
    Role: Literal["Student", "Advisor"]
    Status: Literal["Approved"] = "Approved"  # อนุมัติได้อย่างเดียว (เปลี่ยนเป็นสถานะอื่นทำที่หน้าจัดการบัญชี)


# ผู้รอยืนยันสิทธิ์ = ไม่ถูกระงับ และไม่ใช่ "Approved ที่มีบทบาทแล้ว" (ตรงกับ isAwaitingVerification ฝั่ง frontend)
_AWAITING_VERIFICATION = {"$and": [
    {"Status": {"$ne": "Suspended"}},
    {"$nor": [{"Status": "Approved", "Role": {"$in": ["Student", "Advisor"]}}]},
]}


# ─────────────────────────────────────────
#  GET  /Profile  – ดึงรายชื่อผู้รอยืนยันสิทธิ์ ทีละหน้า
# ─────────────────────────────────────────
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
#  PUT  /Profile/{user_id}  – อนุมัติ (กำหนดบทบาท)
# ─────────────────────────────────────────
@router.put("/Profile/{user_id}")
def approve_user(user_id: str, update: ApproveUser, background_tasks: BackgroundTasks):
    client = Connect_MongoDB()
    try:
        db          = client["BORC"]
        col         = db["UserProfile"]
        col_history = db["AccountManagementHistory"]

        user = col.find_one({"userId": user_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"ไม่พบผู้ใช้งาน userId={user_id}")

        # อนุมัติได้เฉพาะบัญชีที่ยังรอยืนยัน (ตรวจใน filter เดียวกับการอัปเดต กันอนุมัติซ้ำ/ทับบัญชีที่ใช้งานอยู่แล้ว)
        result = col.update_one(
            {"$and": [{"userId": user_id}, *_AWAITING_VERIFICATION["$and"]]},
            {"$set": {"Role": update.Role, "Status": "Approved"}},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=409, detail="บัญชีนี้ไม่ได้อยู่ในสถานะรอยืนยัน (อนุมัติแล้วหรือถูกระงับ) แก้ไขได้ที่หน้าจัดการบัญชี")
        invalidate_user_cache(user_id)  # สิทธิ์/สถานะใหม่ต้องมีผลทันที ไม่รอ cache หมดอายุ

        record_user_history(
            col_history,
            prefix=user.get("Prefix", ""),
            first_name=user.get("Firstname", ""),
            last_name=user.get("Lastname", ""),
            # ใช้ update.Role (ค่าที่เพิ่งเขียนลง DB) ไม่ใช่ user.get("Role") เพราะ user คือเอกสารก่อนอัปเดต
            # ซึ่งบัญชีที่เพิ่งสมัครมี Role="" อยู่แล้ว (key มีจริง) → history จะได้ role ว่างทุกครั้ง
            role=update.Role,
            status_label=VERIFIED,
        )

        # แจ้งเตือน LINE ทั้งอาจารย์ (มีนักศึกษาขอจอง) และนักศึกษาผู้จอง (ยืนยันว่าส่งคำขอแล้ว) — ทำงานใน background ไม่บล็อก response
        if update.Role == "Student":
            background_tasks.add_task(
                notify_chatbot,
                f"{chatbot_uri}/NotifyFristLogin/ApprovedUser/student",
                {
                    "userId" : user_id,
                },
                CHATBOT_INTERNAL_HEADERS
            )
        else:
            pass

        return {
            "message":     "อัพเดตข้อมูลสำเร็จ",
            "userId":      user_id,
            "Role":        update.Role,
            "Status":      "Approved",
            "statusLabel": VERIFIED,
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

import logging
import re

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Query
from Admin.Database.ConnectDB import Connect_MongoDB
from common.user_cache import invalidate_user_cache
from common.attachments import delete_attachments
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.booking_status import ACTIVE_STATUSES
from common.slot_service import recalculate_slot_booked, split_time_range
from bson import ObjectId
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Literal

router = APIRouter()

class UpdateUserRequest(BaseModel):
    id: str
    Prefix: str
    Firstname: str
    Lastname: str
    Role: Literal["Student", "Advisor"]
    Status: Literal["Approved", "Pending", "Suspended"]
    Faculty: str = ""     
    Department: str = ""  

class DeleteUserRequest(BaseModel):
    id: str

# ─────────────────────────────────────────
#  Helper: บันทึก History
# ─────────────────────────────────────────

def _save_history(col_history, first_name: str, last_name: str, role: str, status_label: str):
    col_history.insert_one({
        "firstName":   first_name,
        "lastName":    last_name,
        "role":        role,           # เก็บค่าจริงจาก DB (Student / Advisor)
        "statusLabel": status_label,
        "createdAt":   datetime.now(timezone.utc),
    })


def _attachment_ids(col_booking, booking_filter: dict) -> list:
    """id ของไฟล์แนบทุกไฟล์ในคิวที่กำลังจะถูกลบ (ต้องดึงก่อนลบ) — ลบบัญชีต้องลบเอกสารวิจัยตามไปด้วย"""
    return [
        b["FileId"]
        for b in col_booking.find({**booking_filter, "FileId": {"$nin": [None, ""]}}, {"FileId": 1})
    ]


def _release_slots(db, bookings: list) -> None:
    """คืน slot ของ booking ที่ถูกลบไปแล้ว (นับ booking จริงใหม่ → ไม่มีแล้วจึงปลดล็อก)

    ไม่เรียกตอนลบอาจารย์ เพราะ slot ของอาจารย์ถูกลบไปพร้อมกัน
    """
    for booking in bookings:
        start, end = split_time_range(booking.get("Time", ""))
        date = booking.get("Date", "")
        if booking.get("AdvisorId") and date and start and end:
            recalculate_slot_booked(db, booking["AdvisorId"], date, start, end)


def _cancel_active_bookings_of_advisor(db, active_bookings: list, advisor_name: str) -> None:
    """บันทึกประวัติยกเลิกและแจ้งนักศึกษา สำหรับคิวที่ยังใช้งานอยู่ของอาจารย์ที่กำลังถูกลบ

    cancelledById เป็น id ของนักศึกษา (ตามที่อาจารย์ยกเลิกเอง) เพื่อให้หน้าประวัติของนักศึกษาเห็นรายการนี้
    """
    now = datetime.now(timezone.utc)
    for booking in active_bookings:
        db["CancelBookingHistory"].insert_one({
            "cancelledById"  : booking.get("UserId", ""),
            "cancelledByRole": "Admin",
            "advisorId"      : booking.get("AdvisorId", ""),
            "advisorName"    : advisor_name,
            "studentName"    : booking.get("StudentName", ""),
            "cancelledDate"  : booking.get("Date", ""),
            "cancelReason"   : "บัญชีอาจารย์ถูกลบโดยผู้ดูแลระบบ",
            "status"         : "Cancelled",
            "createdAt"      : now,
            "updatedAt"      : now,
        })
        notify_chatbot(f"{CHATBOT_URL}/NotifyQueueStudent/NotifyStudent", {
            "userId"      : booking.get("UserId", ""),
            "StudentName" : booking.get("StudentName", ""),
            "AdvisorName" : advisor_name,
            "Date"        : booking.get("Date", ""),
            "Time"        : booking.get("Time", ""),
            "Status"      : "Cancelled",
        }, CHATBOT_INTERNAL_HEADERS)


# ─────────────────────────────────────────
#  Helper Functions
# ─────────────────────────────────────────

# สถานะที่หน้า "จัดการบัญชี" แสดง (ตรงกับ MANAGED_STATUSES ฝั่ง frontend)
MANAGED_STATUSES = ["Approved", "Suspended"]


def GetPage_AccountUser(client, page: int, limit: int, search: str) -> dict:
    """ดึงบัญชีที่จัดการได้ทีละหน้า: กรองสถานะ + ค้นหา (ชื่อ/นามสกุล/บทบาท) ที่ MongoDB แล้วแบ่งหน้าด้วย skip/limit

    page เกินหน้าสุดท้ายจะถูกปรับกลับมาที่หน้าสุดท้าย (เช่น ลบรายการสุดท้ายของหน้าไป)
    """
    col = client["BORC"]["UserProfile"]
    query: dict = {"Status": {"$in": MANAGED_STATUSES}}
    term = search.strip()
    if term:
        rx = {"$regex": re.escape(term), "$options": "i"}
        query["$or"] = [{"Firstname": rx}, {"Lastname": rx}, {"Role": rx}]

    total = col.count_documents(query)
    last_page = max(1, -(-total // limit))
    page = min(page, last_page)

    items = []
    for data in col.find(query).sort("_id", 1).skip((page - 1) * limit).limit(limit):
        data["_id"] = str(data["_id"])
        items.append(data)
    return {"items": items, "total": total, "page": page, "limit": limit}


def Delete_AccountUser(client, user_id: str):
    db           = client["BORC"]
    col_profile  = db["UserProfile"]
    col_booking  = db["BookingOnline"]
    col_queue    = db["ManageQueueStudent"]
    col_timeslot = db["ManageTimeSlots"]
    col_history  = db["AccountManagementHistory"]

    try:
        object_id = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="รูปแบบ ID ไม่ถูกต้อง")

    user = col_profile.find_one({"_id": object_id})
    if not user:
        raise HTTPException(status_code=404, detail="ไม่พบบัญชีผู้ใช้นี้ในระบบ")

    role            = user.get("Role", "")
    booking_user_id = user.get("userId", str(object_id))
    first_name      = user.get("Firstname", "")
    last_name       = user.get("Lastname",  "")

    # ========== Student ==========
    if role == "Student":
        # คิวที่ยังใช้งานอยู่ล็อก slot ของอาจารย์ไว้ ต้องคืน slot หลังลบ ไม่งั้นล็อกค้างถาวร
        active_bookings = list(col_booking.find(
            {"UserId": booking_user_id, "Status": {"$in": ACTIVE_STATUSES}}
        ))
        file_ids = _attachment_ids(col_booking, {"UserId": booking_user_id})
        deleted_booking = col_booking.delete_many({"UserId": booking_user_id})
        delete_attachments(db, file_ids)
        _release_slots(db, active_bookings)
        deleted_queue   = col_queue.delete_many({"UserId": booking_user_id})
        deleted_profile = col_profile.delete_one({"_id": object_id})
        invalidate_user_cache(booking_user_id)

        # ✅ ใช้ role จริงจาก DB ("Student")
        _save_history(col_history, first_name, last_name, role=role, status_label="ลบบัญชีแล้ว")

        return {
            "message"         : "ลบบัญชีนักศึกษาเรียบร้อยแล้ว",
            "deleted_bookings": deleted_booking.deleted_count,
            "deleted_queue"   : deleted_queue.deleted_count,
            "deleted_profile" : deleted_profile.deleted_count,
        }

    # ========== Advisor ==========
    elif role == "Advisor":
        prefix       = user.get("Prefix",    "")
        firstname    = user.get("Firstname", "")
        lastname     = user.get("Lastname",  "")
        advisor_name = f"{prefix}{firstname} {lastname}"

        # อิง id ของอาจารย์ ไม่ใช่ชื่อ: อาจารย์ชื่อซ้ำกันต้องไม่ลบคิว/ช่วงเวลาของอีกคนไปด้วย
        active_bookings = list(col_booking.find(
            {"AdvisorId": booking_user_id, "Status": {"$in": ACTIVE_STATUSES}}
        ))
        _cancel_active_bookings_of_advisor(db, active_bookings, advisor_name)

        file_ids = _attachment_ids(col_booking, {"AdvisorId": booking_user_id})
        deleted_booking  = col_booking.delete_many({"AdvisorId": booking_user_id})
        delete_attachments(db, file_ids)
        deleted_timeslot = col_timeslot.delete_many({"advisorId": booking_user_id})
        db["ConsultationAvailability"].delete_many({"advisorId": booking_user_id})
        deleted_profile  = col_profile.delete_one({"_id": object_id})
        invalidate_user_cache(booking_user_id)

        # ✅ ใช้ role จริงจาก DB ("Advisor")
        _save_history(col_history, first_name, last_name, role=role, status_label="ลบบัญชีแล้ว")

        return {
            "message"          : "ลบบัญชีอาจารย์เรียบร้อยแล้ว",
            "advisor_name"     : advisor_name,
            "deleted_bookings" : deleted_booking.deleted_count,
            "deleted_timeslots": deleted_timeslot.deleted_count,
            "deleted_profile"  : deleted_profile.deleted_count,
        }

    else:
        raise HTTPException(status_code=400, detail=f"ไม่รองรับ Role: {role}")


def Update_AccountUser(client, body: UpdateUserRequest):
    db          = client["BORC"]
    col_profile = db["UserProfile"]
    col_booking = db["BookingOnline"]
    col_queue   = db["ManageQueueStudent"]
    col_history = db["AccountManagementHistory"]

    try:
        object_id = ObjectId(body.id)
    except Exception:
        raise HTTPException(status_code=400, detail="รูปแบบ ID ไม่ถูกต้อง")

    user = col_profile.find_one({"_id": object_id})
    if not user:
        raise HTTPException(status_code=404, detail="ไม่พบบัญชีผู้ใช้นี้ในระบบ")

    old_prefix    = user.get("Prefix",    "")
    old_firstname = user.get("Firstname", "")
    old_lastname  = user.get("Lastname",  "")
    old_name      = f"{old_prefix}{old_firstname} {old_lastname}"
    new_name      = f"{body.Prefix}{body.Firstname} {body.Lastname}"
    booking_uid   = user.get("userId", str(object_id))

    # ---- 1) อัปเดต UserProfile ----
    col_profile.update_one(
        {"_id": object_id},
        {"$set": {
            "Prefix"   : body.Prefix,
            "Firstname": body.Firstname,
            "Lastname" : body.Lastname,
            "Role"     : body.Role,
            "Status"   : body.Status,
            "Faculty"   : body.Faculty, 
            "Department": body.Department,  
        }}
    )

    invalidate_user_cache(booking_uid)  # สิทธิ์/สถานะ/ชื่อใหม่ต้องมีผลทันที ไม่รอ cache หมดอายุ

    # ---- 2) อัปเดต Collection ที่เกี่ยวข้อง ----
    if body.Role == "Student":
        col_booking.update_many(
            {"UserId": booking_uid},
            {"$set": {"StudentName": new_name}}
        )
        col_queue.update_many(
            {"UserId": booking_uid},
            {"$set": {"Status": body.Status}}
        )
    elif body.Role == "Advisor":
        # อิง id ของอาจารย์ (ไม่ใช่ชื่อเดิม) และแก้ชื่อใน ManageTimeSlots ด้วย ไม่งั้นนักศึกษาเห็นชื่อเก่า
        col_booking.update_many(
            {"AdvisorId": booking_uid},
            {"$set": {"Advisor_Name": new_name}}
        )
        db["ManageTimeSlots"].update_many(
            {"advisorId": booking_uid},
            {"$set": {"advisor_name": new_name}}
        )
        # ManageQueueStudent เป็นข้อมูลเก่าที่ไม่มี AdvisorId จึงยังจับคู่ด้วยชื่อเดิม
        col_queue.update_many(
            {"Advisor_Name": old_name},
            {"$set": {"Advisor_Name": new_name}}
        )

    # ✅ ใช้ body.Role จริงๆ ("Student" / "Advisor") ไม่แปลงเป็นภาษาไทย
    _save_history(col_history, body.Firstname, body.Lastname, role=body.Role, status_label="แก้ไขบัญชีแล้ว")

    return {"message": "อัปเดตข้อมูลเรียบร้อยแล้ว", "new_name": new_name}


# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────

@router.get("/ShowAccountUser")
def ShowAccountUser(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str = Query("", max_length=100),
):
    """คืนบัญชีทีละหน้าเป็น {items, total, page, limit} (ไม่ส่ง page = หน้า 1)"""
    try:
        client = Connect_MongoDB()
        return GetPage_AccountUser(client, page, limit, search)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/DeleteAccountUser")
def DeleteAccountUser(body: DeleteUserRequest):
    try:
        client = Connect_MongoDB()
        return Delete_AccountUser(client=client, user_id=body.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/UpdateAccountUser")
def UpdateAccountUser(body: UpdateUserRequest):
    try:
        client = Connect_MongoDB()
        return Update_AccountUser(client=client, body=body)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/SyncAllSlots")
def SyncAllSlots():
    """Force-sync booked/isLocked ของทุก slot กับ booking จริง (ใช้เมื่อข้อมูล slot ไม่ตรงกับคิว)

    ย้ายมาจาก /booking/SyncAllSlots ที่เรียกไม่ได้จริง (อยู่ใต้ require_student แต่ตรวจ role Admin/Advisor
    จึงได้ 403 ทุกคน) — ตอนนี้อยู่ใต้ require_admin ของ router นี้ slot ที่อาจารย์ปิดเองไม่ถูกเปิด
    """
    try:
        db = Connect_MongoDB()["BORC"]
        updated = 0
        for doc in db["ManageTimeSlots"].find({}, {"advisorId": 1, "dates": 1}):
            advisor_id = doc.get("advisorId", "")
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list):
                    continue
                for slot in slots:
                    start, end = slot.get("start", ""), slot.get("end", "")
                    if start and end:
                        recalculate_slot_booked(db, advisor_id, date, start, end)
                        updated += 1
        return {"message": f"Sync เสร็จสิ้น อัปเดต {updated} slots"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

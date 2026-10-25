"""หน้า "จัดการบัญชี" ทั้งสองแท็บ ใช้ router ตัวเดียว mount ที่ /ManagementAccount
- แท็บผู้ใช้งาน (นักศึกษา/อาจารย์) ดู แก้ไข ลบ    → สิทธิ์ manage
- แท็บผู้ดูแลระบบ ดูรายการ แก้สิทธิ์ ลบ            → สิทธิ์ add
สิทธิ์ผูกที่แต่ละ endpoint (manage_only / add_only) เพราะสองแท็บใช้สิทธิ์ต่างกัน; main.py ครอบด้วย require_admin อีกชั้น
การอนุมัติผู้สมัครใหม่เป็นของหน้า "ยืนยันการเข้าสู่ระบบ" (LoginVerification.py) การสร้างบัญชีผู้ดูแลระบบเป็นของ AddAdmin.py"""

import logging
import re

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Query
from Admin.Database.ConnectDB import Connect_MongoDB
from Admin.admin_account_rules import admin_col, clean_permissions, log_admin_event, validated_level
from Admin.auth.authAdmin import ADMIN_PERMISSIONS, get_admin_permissions, require_permission, verify_token
from common.account_cleanup import purge_account_data
from common.account_history import DELETED, EDITED, record_user_history
from common.user_cache import invalidate_user_cache
from common.attachments import delete_attachments
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot
from common.booking_status import ACTIVE_STATUSES
from common.year_level import YEAR_LEVELS, parse_year_level, year_level_for_storage
from common.slot_service import recalculate_slot_booked, split_time_range
from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, StrictInt, field_validator
from datetime import datetime, timezone
from typing import Literal, Optional

router = APIRouter()

# สิทธิ์ผูกรายเส้นทาง: แท็บผู้ใช้งาน = manage, แท็บผู้ดูแลระบบ = add
manage_only = [Depends(require_permission("manage"))]
add_only = [Depends(require_permission("add"))]

class UpdateUserRequest(BaseModel):
    id: str
    Prefix: str
    Firstname: str
    Lastname: str
    Role: Literal["Student", "Advisor"]
    Status: Literal["Approved", "Pending", "Suspended"]
    Faculty: str = ""     
    Department: str = ""  
    # ชั้นปี "1"-"4" (รับทั้งข้อความและตัวเลข) — ใช้กับนักศึกษา: ไม่ส่งมา = คงค่าเดิมที่เก็บไว้, ส่งมาแล้วใช้ค่าที่ส่ง
    # อาจารย์ไม่มีชั้นปี (backend ล้างเป็น "" เสมอ); ค่าที่ไม่ใช่ 1-4 (เช่น "abc", 9) → 422
    YearLevel: Optional[StrictInt | str] = None

    @field_validator("YearLevel", mode="after")
    @classmethod
    def _year_level_must_be_offered(cls, value):
        if value is None or value == "":
            return None
        parsed = parse_year_level(value)
        if parsed is None:
            raise ValueError("ชั้นปีต้องเป็น 1 ถึง %d" % max(YEAR_LEVELS))
        return parsed

class DeleteUserRequest(BaseModel):
    id: str

# ─────────────────────────────────────────
#  Helper: บันทึก History
# ─────────────────────────────────────────

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
    prefix_name     = user.get("Prefix",    "")
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
        # แชท/ตำแหน่งที่อ่าน/ประวัติของบัญชีนี้ — ทำก่อนลบโปรไฟล์ เพื่อให้กดลบซ้ำได้ถ้าล้มกลางคัน
        cleanup = purge_account_data(db, booking_user_id, "Student")
        deleted_profile = col_profile.delete_one({"_id": object_id})
        invalidate_user_cache(booking_user_id)

        # ✅ ใช้ role จริงจาก DB ("Student")
        record_user_history(col_history, prefix_name, first_name, last_name, role=role, status_label=DELETED)

        return {
            "message"         : "ลบบัญชีนักศึกษาเรียบร้อยแล้ว",
            "deleted_bookings": deleted_booking.deleted_count,
            "deleted_queue"   : deleted_queue.deleted_count,
            "deleted_profile" : deleted_profile.deleted_count,
            "cleanup"         : cleanup,
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
        cleanup          = purge_account_data(db, booking_user_id, "Advisor")
        deleted_profile  = col_profile.delete_one({"_id": object_id})
        invalidate_user_cache(booking_user_id)

        # ✅ ใช้ role จริงจาก DB ("Advisor")
        record_user_history(col_history, prefix_name, first_name, last_name, role=role, status_label=DELETED)

        return {
            "message"          : "ลบบัญชีอาจารย์เรียบร้อยแล้ว",
            "advisor_name"     : advisor_name,
            "deleted_bookings" : deleted_booking.deleted_count,
            "deleted_timeslots": deleted_timeslot.deleted_count,
            "deleted_profile"  : deleted_profile.deleted_count,
            "cleanup"          : cleanup,
        }

    else:
        raise HTTPException(status_code=400, detail=f"ไม่รองรับ Role: {role}")


def _year_level_to_store(body: UpdateUserRequest, user: dict) -> str:
    """ชั้นปีที่จะเก็บลง UserProfile: อาจารย์ = "" (ล้างชั้นปีเดิมเมื่อเปลี่ยนจากนักศึกษา);
    นักศึกษา ต้องมีชั้นปีที่ถูกต้อง — ใช้ค่าที่ส่งมา หรือค่าที่เก็บไว้เดิมถ้าไม่ได้ส่ง (เช่น อาจารย์เปลี่ยนเป็นนักศึกษาต้องระบุเอง)"""
    if body.Role == "Advisor":
        return year_level_for_storage(None)
    source = body.YearLevel if "YearLevel" in body.model_fields_set else user.get("YearLevel")
    year_level = parse_year_level(source)
    if year_level is None:
        raise HTTPException(status_code=400, detail="กรุณาระบุชั้นปีของนักศึกษา (1-%d)" % max(YEAR_LEVELS))
    return year_level_for_storage(year_level)


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

    year_to_store = _year_level_to_store(body, user)  # ตรวจก่อนเขียนอะไรลงฐานข้อมูล

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
            "YearLevel" : year_to_store,
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
    record_user_history(col_history, body.Prefix, body.Firstname, body.Lastname, role=body.Role, status_label=EDITED)

    return {"message": "อัปเดตข้อมูลเรียบร้อยแล้ว", "new_name": new_name}


# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────

@router.get("/ShowAccountUser", dependencies=manage_only)
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


@router.delete("/DeleteAccountUser", dependencies=manage_only)
def DeleteAccountUser(body: DeleteUserRequest):
    try:
        client = Connect_MongoDB()
        return Delete_AccountUser(client=client, user_id=body.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/UpdateAccountUser", dependencies=manage_only)
def UpdateAccountUser(body: UpdateUserRequest):
    try:
        client = Connect_MongoDB()
        return Update_AccountUser(client=client, body=body)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/SyncAllSlots", dependencies=manage_only)
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


# ═════════════════════════════════════════════════════════════════════════
#  บัญชีผู้ดูแลระบบ (แท็บ "ผู้ดูแลระบบ" ของหน้าจัดการบัญชี) — สิทธิ์ add
# ═════════════════════════════════════════════════════════════════════════

class UpdateAdminPermissions(BaseModel):
    permissions: list[str]
    level      : Optional[str] = None


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


def _load_target_admin(col, admin_id: str, payload: dict) -> dict:
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


@router.get("/ShowAdminAccounts", dependencies=add_only)
def list_admins(payload: dict = Depends(verify_token)):
    try:
        docs = list(admin_col().find({}).sort("_id", 1))
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


@router.put("/UpdateAdminAccount/{admin_id}", dependencies=add_only)
def update_admin_permissions(admin_id: str, data: UpdateAdminPermissions, payload: dict = Depends(verify_token)):
    permissions = clean_permissions(data.permissions)
    level = validated_level(data.level)
    try:
        col = admin_col()
        admin = _load_target_admin(col, admin_id, payload)
        if _is_full(admin) and len(permissions) < len(ADMIN_PERMISSIONS) and not _other_full_admin_exists(col, admin["_id"]):
            raise HTTPException(status_code=409, detail="ต้องเหลือผู้ดูแลระบบที่มีสิทธิ์เต็มอย่างน้อย 1 คน")
        changes = {"Permissions": permissions, "updatedAt": datetime.now(timezone.utc)}
        if level is not None:  # ไม่ส่งระดับมา = คงป้ายเดิมไว้
            changes["Level"] = level
        col.update_one({"_id": admin["_id"]}, {"$set": changes})
        log_admin_event(EDITED, admin.get("Email", ""), payload["email"], permissions, level or admin.get("Level"))
    except HTTPException:
        raise
    except Exception:
        logger.exception("แก้สิทธิ์ผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"message": "แก้สิทธิ์ผู้ดูแลระบบสำเร็จ", "permissions": permissions}


@router.delete("/DeleteAdminAccount/{admin_id}", dependencies=add_only)
def delete_admin(admin_id: str, payload: dict = Depends(verify_token)):
    try:
        col = admin_col()
        admin = _load_target_admin(col, admin_id, payload)
        if _is_full(admin) and not _other_full_admin_exists(col, admin["_id"]):
            raise HTTPException(status_code=409, detail="ต้องเหลือผู้ดูแลระบบที่มีสิทธิ์เต็มอย่างน้อย 1 คน")
        col.delete_one({"_id": admin["_id"]})
        log_admin_event(DELETED, admin.get("Email", ""), payload["email"])  # session ของบัญชีนี้ใช้ไม่ได้ทันที: verify_token ตรวจ DB ทุกคำขอ
    except HTTPException:
        raise
    except Exception:
        logger.exception("ลบบัญชีผู้ดูแลระบบไม่สำเร็จ")
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"message": "ลบบัญชีผู้ดูแลระบบสำเร็จ"}

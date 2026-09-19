import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException
from Admin.Database.ConnectDB import Connect_MongoDB
from common.user_cache import invalidate_user_cache
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


# ─────────────────────────────────────────
#  Helper Functions
# ─────────────────────────────────────────

def GetData_AccountUser(client):
    db  = client["BORC"]
    col = db["UserProfile"]

    result = []
    for data in col.find():
        data["_id"] = str(data["_id"])
        result.append(data)
    return result


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
        deleted_booking = col_booking.delete_many({"UserId": booking_user_id})
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

        deleted_booking  = col_booking.delete_many({"Advisor_Name": advisor_name})
        deleted_timeslot = col_timeslot.delete_many({"advisor_name": advisor_name})
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
        col_booking.update_many(
            {"Advisor_Name": old_name},
            {"$set": {"Advisor_Name": new_name}}
        )
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
async def ShowAccountUser():
    try:
        client = Connect_MongoDB()
        return GetData_AccountUser(client=client)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/DeleteAccountUser")
async def DeleteAccountUser(body: DeleteUserRequest):
    try:
        client = Connect_MongoDB()
        return Delete_AccountUser(client=client, user_id=body.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/UpdateAccountUser")
async def UpdateAccountUser(body: UpdateUserRequest):
    try:
        client = Connect_MongoDB()
        return Update_AccountUser(client=client, body=body)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

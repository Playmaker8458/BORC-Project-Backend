import logging

logger = logging.getLogger(__name__)
import os
import cloudinary
import cloudinary.uploader

from fastapi import APIRouter, HTTPException, status, Depends, File, UploadFile
from pydantic import BaseModel
from ..Database.ConnectDB import Connect_MongoDB
from common.user_cache import invalidate_user_cache
from datetime import datetime, timezone
from users.auth.authUser import verify_user_token

router = APIRouter()

# ✅ Config Cloudinary
cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.getenv("CLOUDINARY_API_KEY"),
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
)


# ─── Models ───────────────────────────────────────────────────────────────────

class UpdateProfileName(BaseModel):
    Prefix    : str
    Firstname : str
    Lastname  : str


# ─── Helpers ──────────────────────────────────────────────────────────────────

def update_UserNameDB(user_id: str, prefix: str, firstname: str, lastname: str) -> bool:
    try:
        db  = Connect_MongoDB()["BORC"]
        col = db["UserProfile"]
        
        # 1. Fetch user's role
        user_data = col.find_one({"userId": user_id})
        if not user_data:
            return False
            
        role = user_data.get("Role")
        new_name = f"{prefix}{firstname} {lastname}"
        
        # 2. Update UserProfile
        result = col.update_one(
            {"userId": user_id},
            {
                "$set": {
                    "Prefix"    : prefix,
                    "Firstname" : firstname,
                    "Lastname"  : lastname,
                    "updatedAt" : datetime.now(timezone.utc)
                }
            }
        )
        
        invalidate_user_cache(user_id)  # ชื่อใหม่ต้องมีผลทันที ไม่รอ cache หมดอายุ

        # 3. Cascade updates to sync names across related collections
        # ใช้ matched_count เพื่อ sync ข้อมูลที่อาจค้างจากการแก้ไขก่อนหน้าได้ด้วย
        if result.matched_count > 0:
            if role == "Student":
                db["BookingOnline"].update_many(
                    {"UserId": user_id},
                    {"$set": {"StudentName": new_name}}
                )
            elif role == "Advisor":
                db["BookingOnline"].update_many(
                    {"AdvisorId": user_id},
                    {"$set": {"Advisor_Name": new_name}}
                )
                db["ManageTimeSlots"].update_many(
                    {"advisorId": user_id},
                    {"$set": {"advisor_name": new_name}}
                )
                
        return result.matched_count > 0
    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในการอัพเดตชื่อ: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


def update_UserImageDB(user_id: str, image_url: str) -> bool:
    try:
        db  = Connect_MongoDB()["BORC"]
        col = db["UserProfile"]
        result = col.update_one(
            {"userId": user_id},
            {
                "$set": {
                    "imageURL"    : image_url,
                    "imageSource" : "upload",
                    "updatedAt"   : datetime.now(timezone.utc)
                }
            }
        )
        invalidate_user_cache(user_id)
        return result.matched_count > 0
    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในการอัพเดตรูปภาพ: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Routes ───────────────────────────────────────────────────────────────────

# ✅ PATCH /UpdateProfileName
@router.patch("/UpdateProfileName")
def update_profile_name(
    data    : UpdateProfileName,
    payload : dict = Depends(verify_user_token)
):
    if not data.Firstname.strip() or not data.Lastname.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="กรุณากรอกชื่อและนามสกุลให้ครบถ้วน"
        )

    updated = update_UserNameDB(
        payload["user_id"],
        data.Prefix,
        data.Firstname.strip(),
        data.Lastname.strip()
    )

    if not updated:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ใช้")

    return {
        "message"   : "อัพเดตชื่อสำเร็จ",
        "Prefix"    : data.Prefix,
        "Firstname" : data.Firstname.strip(),
        "Lastname"  : data.Lastname.strip()
    }


# ✅ PATCH /UpdateProfileImage
@router.patch("/UpdateProfileImage")
async def update_profile_image(
    file    : UploadFile = File(...),
    payload : dict       = Depends(verify_user_token)
):
    ALLOWED_TYPES = ["image/jpeg", "image/png", "image/webp"]
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="รองรับเฉพาะไฟล์ .jpg, .png, .webp เท่านั้น"
        )

    MAX_SIZE = 5 * 1024 * 1024
    contents = await file.read()
    if len(contents) > MAX_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ขนาดไฟล์ต้องไม่เกิน 5MB"
        )

    try:
        upload_result = cloudinary.uploader.upload(
            contents,
            folder         = "BORC/profiles",
            public_id      = f"user_{payload['user_id']}",
            overwrite      = True,
            resource_type  = "image",
            transformation = [
                {"width": 400, "height": 400, "crop": "fill", "gravity": "face"},
                {"quality": "auto"}
            ]
        )

        new_image_url = upload_result["secure_url"]

        updated = update_UserImageDB(payload["user_id"], new_image_url)
        if not updated:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ใช้")

        return {
            "message"  : "อัพเดตรูปโปรไฟล์สำเร็จ",
            "imageUrl" : new_image_url
        }

    except cloudinary.exceptions.Error as e:
        logger.exception("เกิดข้อผิดพลาดในการอัพโหลดรูปภาพ: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )

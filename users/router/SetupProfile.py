import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request, status, Response
from pydantic import BaseModel, field_validator
from typing import Optional
from users.Database.ConnectDB import Connect_MongoDB
import os
from datetime import datetime, timedelta, timezone
from common.jwt_utils import encode_token
from common.rate_limit import limit
from users.auth.authUser import verify_pending_or_active_user, set_user_session, SESSION_MAX_AGE

router = APIRouter()

COOKIE_NAME = "access_token"
IS_PROD = os.getenv("ENV") == "production"

# คณะ/สาขา ที่เปิดรับสมัครจริง — ต้องตรงกับรายการฝั่ง frontend
# (src/routes/auth/ProfileSetup.tsx: FACULTIES) หน้าที่นี้ไม่ยอมรับค่าที่ไม่อยู่ใน
# รายการนี้ เพื่อกัน user ยิง faculty/department มั่ว ๆ เข้า DB ตรง ๆ ผ่าน API
FACULTIES: dict[str, list[str]] = {
    "วิทยาศาสตร์": ["วิทยาการคอมพิวเตอร์และปัญญาประดิษฐ์"],
}

_MAX_NAME_LEN = 100


class DataProfile(BaseModel):
    """Payload ของ endpoint สาธารณะ /AddDataProfile (ผู้ใช้กรอกเองหลัง LINE login)

    ตั้งใจไม่มี field `role` — บทบาท (Student/Advisor) เป็นสิ่งที่ Admin เท่านั้น
    ที่กำหนดได้ผ่านหน้า ManagementAccount/LoginVerification (ดู
    Admin/router/ManagementAccount.py: UpdateAccountUser) ไม่ใช่สิ่งที่ผู้สมัคร
    เลือกเองได้ตอนลงทะเบียน — ถ้ายอมให้ client ส่ง role มาแล้วเก็บตรง ๆ จะเปิดช่อง
    ให้ user เลือกตั้งตัวเองเป็น Advisor ได้ตั้งแต่ตอนสมัคร (privilege
    self-assignment) แม้ Admin จะยังต้องกด Approve ก็ตาม.
    """

    prefix    : str
    firstname : str
    lastname  : str
    userID    : str
    faculty   : str
    department: str
    imageURL  : Optional[str] = "line"

    @field_validator("prefix", "firstname", "lastname", mode="after")
    @classmethod
    def _strip_and_bound(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ห้ามเว้นว่าง")
        if len(value) > _MAX_NAME_LEN:
            raise ValueError(f"ความยาวต้องไม่เกิน {_MAX_NAME_LEN} ตัวอักษร")
        return value

    @field_validator("faculty", mode="after")
    @classmethod
    def _faculty_must_exist(cls, value: str) -> str:
        if value not in FACULTIES:
            raise ValueError("ไม่พบคณะที่เลือกในระบบ")
        return value

    @field_validator("department", mode="after")
    @classmethod
    def _department_must_belong_to_faculty(cls, value: str, info) -> str:
        faculty = info.data.get("faculty")
        if faculty is None or value not in FACULTIES.get(faculty, []):
            raise ValueError("สาขาวิชาไม่ตรงกับคณะที่เลือก")
        return value


def AddData_Database(client_data, data: DataProfile):
    db  = client_data["BORC"]
    col = db["UserProfile"]
    existing_user = col.find_one({"userId": data.userID})
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="มีข้อมูลผู้ใช้นี้อยู่ในระบบแล้ว"
        )

    now = datetime.now(timezone.utc)

    new_user = {
        "Prefix"        : data.prefix,
        "Firstname"     : data.firstname,
        "Lastname"      : data.lastname,
        "userId"        : data.userID,
        "imageURL"      : data.imageURL,
        # Role ว่างเสมอตอนสมัครเอง — Admin เป็นคนกำหนดทีหลังตอน Approve
        "Role"          : "",
        "Status"        : "Pending",
        "Faculty"       : data.faculty,
        "Department"    : data.department,
        "isActive"      : True,
        "deactivatedAt" : None,
        "deactivatedBy" : None,
        "createdAt"     : now,
        "updatedAt"     : now
    }

    result = col.insert_one(new_user)
    if not result.inserted_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ไม่สามารถเพิ่มข้อมูลผู้ใช้ได้ กรุณาลองใหม่อีกครั้ง"
        )

    return new_user


@router.post('/AddDataProfile')
@limit("10/minute")
async def AddDataProfile(
    request: Request,
    data: DataProfile,
    response: Response,
    session: dict = Depends(verify_pending_or_active_user),
):
    if session.get("user_id") != data.userID or not session.get("registration"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="session สำหรับลงทะเบียนไม่ถูกต้อง")

    myclient = Connect_MongoDB()
    try:
        new_user = AddData_Database(client_data=myclient, data=data)

        token_payload = {
            "user_id"   : data.userID,
            "role"      : new_user["Role"],
            "Prefix"    : data.prefix,
            "Firstname" : data.firstname,
            "Lastname"  : data.lastname,
            "Faculty"   : data.faculty,
            "Department": data.department,
            "Status"    : "Pending",
            "imageURL"  : data.imageURL,
        }

        # ใช้ SESSION_MAX_AGE เดียวกับ cookie (authUser.set_user_session) — เดิม JWT
        # ในนี้อายุ 7 วันแต่ cookie ถูกลบไปแล้วตั้งแต่วันที่ 1 (max_age=SESSION_MAX_AGE)
        # ทำให้ token ยังใช้ยืนยันตัวตนต่อได้อีก 6 วันถ้าคุกกี้หลุดไปอยู่ในมือคนอื่น
        # (เช่นโดน XSS ขโมยไปก่อนเบราว์เซอร์ลบทิ้ง)
        access_token = encode_token(token_payload, SESSION_MAX_AGE)

        set_user_session(request, response, access_token)

        return {"message": "เพิ่มข้อมูลผู้ใช้ใหม่เสร็จสิ้น"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในระบบ: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")
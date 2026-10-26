import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request, status, Response, BackgroundTasks
from pydantic import BaseModel, StrictInt
from typing import Optional
from pymongo.errors import DuplicateKeyError
from users.Database.ConnectDB import Connect_MongoDB
import os
from datetime import datetime, timezone
from common.consent import consent_fields
from common.year_level import year_level_for_storage
from common.jwt_utils import encode_token
from common.rate_limit import limit
from users.auth.authUser import verify_pending_or_active_user, set_user_session, SESSION_MAX_AGE

from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot


router = APIRouter()

chatbot_uri = CHATBOT_URL

COOKIE_NAME = "access_token"
IS_PROD = os.getenv("ENV") == "production"


class DataProfile(BaseModel):
    prefix    : str
    firstname : str
    lastname  : str
    userID    : str
    faculty   : str
    department: str
    imageURL  : Optional[str] = "line"
    yearLevel : Optional[StrictInt | str] = None
    consentVersion: str


def AddData_Database(client_data, data):
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
        # เก็บเป็นข้อความ "1"-"4" ตามที่เลือกในหน้า ProfileSetup; "" = ไม่ระบุ (อาจารย์ / ไม่ได้เลือก) — ลำดับฟิลด์ตามเอกสารเดิมใน Atlas
        "YearLevel"     : year_level_for_storage(data.yearLevel),
        "Faculty"       : data.faculty,
        "Department"    : data.department,
        "isActive"      : True,
        "deactivatedAt" : None,
        "deactivatedBy" : None,
        "createdAt"     : now,
        "updatedAt"     : now,
        **consent_fields(now),
    }

    try:
        col.insert_one(new_user)
    except DuplicateKeyError:
        # คำขอสมัครซ้ำที่ผ่านการเช็ก find ด้านบนพร้อมกัน (unique index userId ใน common/indexes.py)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="มีข้อมูลผู้ใช้นี้อยู่ในระบบแล้ว"
        )


@router.post('/AddDataProfile')
@limit("10/minute")
async def AddDataProfile(
    request: Request,
    data: DataProfile,
    response: Response,
    background_tasks: BackgroundTasks,
    session: dict = Depends(verify_pending_or_active_user)
):
    if session.get("user_id") != data.userID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="session สำหรับลงทะเบียนไม่ถูกต้อง")

    # verify_pending_or_active_user ปล่อยผ่านได้ 2 แบบ: registration token (ผู้ใช้ใหม่)
    # หรือ token เต็มของผู้ใช้ที่มีข้อมูลใน DB แล้ว — แบบหลังแปลว่าสมัครไปแล้ว
    # จึงตอบ 409 (สมัครซ้ำ) แทน 403 เพื่อให้ frontend แยกจาก "session ผิด" ได้
    if not session.get("registration"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="มีข้อมูลผู้ใช้นี้อยู่ในระบบแล้ว",
        )

    myclient = Connect_MongoDB()
    try:
        token_payload = {
            "user_id"   : data.userID,
            "role"      : "",
            "Prefix"    : data.prefix,
            "Firstname" : data.firstname,
            "Lastname"  : data.lastname,
            "Faculty"   : data.faculty,
            "Department": data.department,
            "Status"    : "Pending",
            "imageURL"  : data.imageURL,
        }

        background_tasks.add_task(
            notify_chatbot,
            f"{chatbot_uri}/ApprovedUser/login",
            {
                "userId" : data.userID,
                "Role" : "Pending"
            },
            CHATBOT_INTERNAL_HEADERS
        )

        AddData_Database(client_data=myclient, data=data)

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
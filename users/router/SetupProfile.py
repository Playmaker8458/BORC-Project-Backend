import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request, status, Response, BackgroundTasks
from pydantic import BaseModel, StrictInt, field_validator, model_validator
from typing import Optional
from pymongo.errors import DuplicateKeyError
from users.Database.ConnectDB import Connect_MongoDB
import os
from datetime import datetime, timezone
from common.consent import consent_fields, validate_consent_version
from common.url_safety import UnsafeUrl, validate_https_url
from common.year_level import YEAR_LEVELS, parse_year_level, year_level_for_storage
from common.jwt_utils import encode_token
from common.rate_limit import limit
from users.auth.authUser import verify_pending_or_active_user, set_user_session, SESSION_MAX_AGE

from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL, notify_chatbot


router = APIRouter()

chatbot_uri = CHATBOT_URL

COOKIE_NAME = "access_token"
IS_PROD = os.getenv("ENV") == "production"


# คณะ -> สาขาที่เลือกได้ตอนสมัคร — ต้องตรงกับ frontend: src/lib/constants/profile.ts: FACULTIES
FACULTIES = {
    "วิทยาศาสตร์": [
        "วิทยาการคอมพิวเตอร์และปัญญาประดิษฐ์", "การประกอบอาหารและการจัดการงานครัว",
        "วิทยาศาสตร์การกีฬาและการออกกำลังกาย", "เทคโนโลยีสารสนเทศและนวัตกรรมดิจิทัล",
        "การจัดการสิ่งแวดล้อมและความปลอดภัย", "วิศวกรรมการผลิตและการจัดการพลังงาน",
        "วิศวกรรมโยธาและบริหารงานก่อสร้าง", "เทคโนโลยีอิเล็กทรอนิกส์อัจฉริยะและหุ่นยนต์",
        "คณิตศาสตร์ (ค.บ. 4 ปี)", "วิทยาศาสตร์ทั่วไป (ค.บ. 4 ปี)", "เคมี (ค.บ. 4 ปี)",
        "ชีววิทยา (ค.บ. 4 ปี)", "คอมพิวเตอร์ (ค.บ. 4 ปี)", "มัลติมีเดียและอีสปอร์ต",
        "การประกันภัยและการบริหารความเสี่ยง", "การแพทย์แผนจีน",
    ],
    "เกษตรและชีวภาพ": ["วิทยาศาสตร์และเทคโนโลยีการอาหาร",
        "วิทยาศาสตร์เกษตรเพื่อสุขภาวะ",
        "เทคโนโลยีการเกษตรสมัยใหม่",
    ],
    "ศึกษาศาสตร์": [
        "การศึกษาปฐมวัย (ค.บ. 4 ปี)",
        "พลศึกษา (ค.บ. 4 ปี)",
        "เทคโนโลยีดิจิทัลเพื่อการศึกษา (ค.บ. 4 ปี)",
        "จิตวิทยา",
    ],
    "มนุษยศาสตร์และสังคมศาสตร์": [
        "ภาษาไทย","ภาษาญี่ปุ่นธุรกิจ",
        "ภาษาอังกฤษ",
        "ภาษาอังกฤษ (ค.บ. 4 ปี)",
        "สังคมศึกษา (ค.บ. 4 ปี)",
        "ภาษาไทย (ค.บ. 4 ปี)",
        "ภาษาจีน (ค.บ. 4 ปี)",
        "นิติศาสตร์",
        "การบริการการบิน",
        "ประวัติศาสตร์กับการจัดการวัฒนธรรมและท่องเที่ยว",
        "ภาษาจีนธุรกิจ",
        "นาฏศิลป์และการออกแบบการแสดง",
        "ศิลปะและการออกแบบ",
        "รัฐประศาสนศาสตร์",
        "ดนตรีสากล",
        "การพัฒนาชุมชนและสังคม",
    ],
    "วิทยาการจัดการ": [
        "การจัดการท่องเที่ยว การบริการและอีเวนต์",
        "การตลาดดิจิทัล",
        "คอมพิวเตอร์และเทคโนโลยีธุรกิจดิจิทัล",
        "การจัดการทรัพยากรมนุษย์และองค์กรดิจิทัล",
        "การจัดการธุรกิจดิจิทัล",
        "นิเทศศาสตรบัณฑิต",
        "บัญชีบัณฑิต",
        "การเงินและการลงทุนยุคดิจิทัล",
    ],
    "บัณฑิตวิทยาลัย": [
        "นาฏศิลป์ศึกษา",
        "เทคโนโลยีธุรกิจดิจิทัล",
        "บริหารธุรกิจ",
        "การบริหารการศึกษา",
        "เทคโนโลยีดนตรีสร้างสรรค์",
        "หลักสูตรและการสอน",
    ],
}

_MAX_NAME_LEN = 100       # ตรงกับหน้าแก้ไขโปรไฟล์ (SettingProfile.py)
_MAX_IMAGE_URL_LEN = 500


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

    @field_validator("prefix", "firstname", "lastname", mode="after")
    @classmethod
    def _bounded_name(cls, value: str) -> str:
        if len(value) > _MAX_NAME_LEN:
            raise ValueError(f"ความยาวต้องไม่เกิน {_MAX_NAME_LEN} ตัวอักษร")
        return value

    @field_validator("imageURL", mode="after")
    @classmethod
    def _safe_image_url(cls, value):
        # None/"" /"line" = ไม่มีรูป (ใช้ค่าเริ่มต้น); อย่างอื่นต้องเป็น https ที่ปลอดภัยและไม่ยาวเกิน
        if value is None:
            return "line"
        if value in ("", "line"):
            return value
        if len(value) > _MAX_IMAGE_URL_LEN:
            raise ValueError("ลิงก์รูปภาพยาวเกินไป")
        try:
            return validate_https_url(value)
        except UnsafeUrl as exc:
            raise ValueError(str(exc)) from None

    @field_validator("yearLevel", mode="after")
    @classmethod
    def _year_level_must_be_offered(cls, value):
        if value is None or value == "":
            return None
        parsed = parse_year_level(value)
        if parsed is None:
            raise ValueError("ชั้นปีต้องเป็น 1 ถึง %d" % max(YEAR_LEVELS))
        return parsed

    @field_validator("consentVersion", mode="after")
    @classmethod
    def _must_be_current(cls, value: str) -> str:
        return validate_consent_version(value)

    @model_validator(mode="after")
    def _department_belongs_to_faculty(self):
        if self.faculty not in FACULTIES:
            raise ValueError("ไม่พบคณะที่เลือก")
        if self.department not in FACULTIES[self.faculty]:
            raise ValueError("สาขาไม่อยู่ในคณะที่เลือก")
        return self


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
            f"{chatbot_uri}/NotifyFristLogin/ApprovedUser/login",
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
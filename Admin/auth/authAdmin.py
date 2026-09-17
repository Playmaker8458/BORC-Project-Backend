import logging
import os
from datetime import timedelta

from dotenv import load_dotenv

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from bson import ObjectId
from bson.errors import InvalidId

from Admin.Database.ConnectDB import Connect_MongoDB
from common.jwt_utils import encode_token, decode_token, JWTError
from common.rate_limit import limiter, limit
import bcrypt

logger = logging.getLogger(__name__)

# สร้าง router (แทน blueprint ของ fast API)
router = APIRouter()

# ตั้งค่าการสร้าง JWT (การเข้ารหัส/ถอดรหัสจริงอยู่ใน common/jwt_utils.py)
ACCESS_TOKEN_EXPIRE_MINUTES =  60 * 24  # 24 ชั่วโมง
# เข้าถึงตัวแปรในไฟล์ .env เพื่อดึงมาใช้งานในไฟล์ auth.py แบบ local
load_dotenv()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/authAdmin/Login")

# MODEL รูปแบบข้อมูลที่ client ต้องส่งมา
class LoginRequest(BaseModel):
    email: str
    password: str

# ฟังก์ชันสร้าง JWT token
def create_access_token(data: dict):
    """
    รับข้อมูล user แล้วสร้าง token พร้อมวันหมดอายุ
    (ใช้ common.jwt_utils.encode_token ร่วมกับฝั่ง users/auth/authUser.py)
    """
    return encode_token(data, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))


# ตรวจสอบ Token ว่ามีหรือไม่
def verify_token(token: str = Depends(oauth2_scheme)):
    try:
        payload = decode_token(token)
        email   = payload.get("sub")
        user_id = payload.get("user_id")
        role    = payload.get("role")
        fullName = payload.get("FullName")

        if email is None or user_id is None or role is None or fullName is None:
            raise HTTPException(status_code=401, detail="Token ไม่ถูกต้อง")

        # ตรวจสอบซ้ำกับฐานข้อมูลทุกครั้ง เพื่อกันกรณีบัญชี Admin ถูกลบไปแล้ว
        # แต่ Token เดิม (อายุ 24 ชม.) ยังไม่หมดอายุ
        admin = getAdminByIdDB(user_id)
        if admin is None:
            raise HTTPException(status_code=401, detail="บัญชีผู้ใช้งานนี้ถูกลบหรือไม่มีอยู่ในระบบแล้ว")

        return {"email": email, "user_id": user_id, "role": role, "FullName" : fullName}

    except HTTPException:
        raise
    except JWTError:
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้องหรือหมดอายุ")


def require_admin(payload: dict = Depends(verify_token)):
    if payload.get("role") != "Admin":
        raise HTTPException(status_code=403, detail="หน้านี้สำหรับผู้ดูแลระบบเท่านั้น")
    return payload


def getAdminDB(data: str) -> dict | None:
    db  = Connect_MongoDB()["BORC"]
    col = db["LoginAdmin"]
    return col.find_one({"Email": data.email})


def getAdminByIdDB(admin_id: str) -> dict | None:
    db  = Connect_MongoDB()["BORC"]
    col = db["LoginAdmin"]
    try:
        object_id = ObjectId(admin_id)
    except (InvalidId, TypeError):
        return None
    return col.find_one({"_id": object_id})


# Login มีหน้าที่ตรวจสอบ Email, Password ว่าตรงกับฐานข้อมูลหรือไม่
@router.post("/Login")
@limit("5/minute")
def login(request: Request, data: LoginRequest):
    if not data.email or not data.password:
        raise HTTPException(status_code=400, detail="ไม่มี Username และ Password กรอกเข้ามา")

    try:
        # ตรวจสอบข้อมูล Admin ในการเข้าสู่ระบบ
        admin = getAdminDB(data)
        if admin:
            hashed_password = admin["Password"]

            if not bcrypt.checkpw(
                data.password.encode("utf-8"),
                hashed_password.encode("utf-8")
            ):
                raise HTTPException(status_code=401, detail="รหัสผ่านไม่ถูกต้องกรุณากรอกใหม่อีกครั้ง")

            access_token = create_access_token({
                "sub"     : admin["Email"],
                "user_id" : str(admin["_id"]),
                "role"    : "Admin",
                "FullName": "ผู้ดูแลระบบ" 
            })

            return {"access_token": access_token}


        # ถ้าไม่พบบัญชีผู้ใช้งานจะแสดงสถานะ 404 กลับไป
        raise HTTPException(status_code=404, detail="ไม่พบบัญชีผู้ใช้งานในระบบ")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


#  ตรวจสอบว่า Login อยู่ไหม 
@router.get("/me")
def get_me(payload: dict = Depends(verify_token)):
    return {
        "FullName": payload["FullName"],
        "user_id" : payload["user_id"],
        "role"    : payload["role"]
    }


# ── ROUTE: Admin Dashboard ─────────────────────────
@router.get("/admin/dashboard")
def admin_dashboard(payload: dict = Depends(verify_token)):
    return {"message": payload["FullName"]}

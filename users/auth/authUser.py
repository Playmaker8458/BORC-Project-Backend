import logging
import os
from datetime import datetime, timedelta, timezone

import requests  # noqa: F401  (re-export: tests patch authUser.requests.post for LINE calls)
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from users.Database.ConnectDB import Connect_MongoDB
from common.cookies import cookie_security_flags
from common.jwt_utils import encode_token, decode_token, JWTError
from common.user_cache import cache_get, cache_put, invalidate_user_cache  # noqa: F401  (re-export)
from common.rate_limit import limit
from .line_oauth import (  # noqa: F401  (re-export: LINE_LOGIN_* kept for any external reference)
    LINE_LOGIN_CHANNEL_ID,
    LINE_LOGIN_CHANNEL_SECRET,
    LINE_LOGIN_REDIRECT_URI,
    exchange_code_for_access_token,
    verify_line_token,
)

logger = logging.getLogger(__name__)


# ============================================================
# Router / Environment
# ============================================================

router = APIRouter()

load_dotenv()

IS_PROD = os.getenv("ENV") == "production"


# ============================================================
# JWT Configuration
# ============================================================

ALGORITHM = "HS256"
COOKIE_NAME = "access_token"
SESSION_MAX_AGE = timedelta(days=1)
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is not set in environment variables")


# ============================================================
# User Status
# ============================================================

ACTIVE_STATUS = "Approved"

BLOCKED_STATUSES = {
    "Suspended",
    "Rejected",
    "Banned",
}


# ============================================================
# Pydantic Models
# ============================================================

class LineAuthCode(BaseModel):
    """
    Authorization Code ที่ได้รับจาก LINE Login
    """

    code: str


# ============================================================
# Cookie Security
# ============================================================

def _cookie_security_flags(request: Request) -> dict:
    """secure/samesite ของคุกกี้ session — ตัวจริงอยู่ที่ common/cookies.py (ใช้ร่วมกับคุกกี้แอดมิน)

    หมายเหตุ: cross-site cookie (SameSite=None) ใช้งานได้เฉพาะบน HTTPS เท่านั้น
    (ข้อบังคับของเบราว์เซอร์ ไม่ใช่ข้อจำกัดของโค้ดนี้)
    """

    return cookie_security_flags(request)


# ============================================================
# MongoDB
# ============================================================

def get_UserDB(UUID: str) -> dict | None:
    """
    ค้นหาข้อมูล User จาก MongoDB
    """

    db = Connect_MongoDB()["BORC"]
    collection = db["UserProfile"]

    return collection.find_one({"userId": UUID}, {"_id": 0})


# ============================================================
# Create User Session
# ============================================================

def set_user_session(request: Request, response: Response, token: str):
    """
    สร้าง HttpOnly Cookie
    สำหรับเก็บ JWT Session
    """

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=int(SESSION_MAX_AGE.total_seconds()),
        path="/",
        **_cookie_security_flags(request),
    )


# ============================================================
# Short-lived cache for per-request user lookup (ดู common/user_cache.py)
# ============================================================

def _get_user_cached(user_id: str) -> dict | None:
    user = cache_get(user_id)
    if user is not None:
        return user
    user = get_UserDB(user_id)
    # cache เฉพาะบัญชีที่อนุมัติแล้ว: ผู้ใช้ที่เพิ่งได้รับอนุมัติต้องเข้าใช้งานได้ทันที
    if user and user.get("Status") == ACTIVE_STATUS:
        cache_put(user_id, user)
    return user


# ============================================================
# Verify User JWT
# ============================================================

def verify_user_token(request: Request):
    """
    ตรวจสอบ JWT จาก Cookie

    ตรวจสอบ:
        1. มี Cookie หรือไม่
        2. JWT ถูกต้องหรือไม่
        3. User มีอยู่ใน DB หรือไม่
        4. Role ถูกต้องหรือไม่
        5. Status เป็น Approved หรือไม่
    """

    token = request.cookies.get(COOKIE_NAME)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ไม่พบ Token กรุณาเข้าสู่ระบบ",
        )

    try:
        payload = decode_token(token)

        user_id = payload.get("user_id")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token ไม่ถูกต้อง",
            )

        db_user = _get_user_cached(user_id)

        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ไม่พบข้อมูลผู้ใช้",
            )

        role = db_user.get("Role")

        if role not in ["Student", "Advisor"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ไม่มีสิทธิ์เข้าถึง",
            )

        if db_user.get("Status") != ACTIVE_STATUS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="บัญชีของคุณไม่ได้รับการอนุมัติ หรือถูกระงับ กรุณาติดต่อผู้ดูแลระบบ",
            )

        return {
            "user_id": user_id,
            "role": role,
            "Status": db_user.get("Status", ""),
            "Prefix": db_user.get("Prefix", ""),
            "Firstname": db_user.get("Firstname", ""),
            "Lastname": db_user.get("Lastname", ""),
            "imageURL": db_user.get("imageURL", ""),
        }

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token ไม่ถูกต้อง หรือหมดอายุ",
        )

def _require_role(request: Request, role: str, detail: str) -> dict:
    payload = verify_user_token(request)
    if not payload or "user_id" not in payload:
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้องหรือหมดอายุ")
    if payload.get("role") != role:
        raise HTTPException(status_code=403, detail=detail)
    return payload


def get_current_advisor(request: Request) -> dict:
    """verify_user_token + บังคับว่าต้องเป็นอาจารย์ (ใช้แทนสำเนาที่เคยมีในแต่ละ router)"""
    return _require_role(request, "Advisor", "ใช้งานได้เฉพาะอาจารย์เท่านั้น")


def get_current_student(request: Request) -> dict:
    """verify_user_token + บังคับว่าต้องเป็นนักศึกษา"""
    return _require_role(request, "Student", "ใช้งานได้เฉพาะนักศึกษาเท่านั้น")



def get_user_id(payload: dict) -> str | None:
    """ดึง user id จาก payload ที่ได้จาก verify_user_token/decode_token.

    verify_user_token คืน key "user_id" เสมอ ส่วน "userId"/"sub" เป็น fallback
    ไว้เผื่อ payload มาจาก token รูปแบบอื่น (เช่น ระหว่าง migration รูปแบบ token)
    """

    return payload.get("user_id") or payload.get("userId") or payload.get("sub")


def require_student(payload: dict = Depends(verify_user_token)):
    """อนุญาตเฉพาะบัญชีนักศึกษาที่ได้รับอนุมัติแล้ว."""

    if payload["role"] != "Student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="หน้านี้สำหรับนักศึกษาเท่านั้น",
        )

    return payload


def require_advisor(payload: dict = Depends(verify_user_token)):
    """อนุญาตเฉพาะบัญชีอาจารย์ที่ได้รับอนุมัติแล้ว."""

    if payload["role"] != "Advisor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="หน้านี้สำหรับอาจารย์เท่านั้น",
        )

    return payload


def ensure_user_role(payload: dict, required_role: str):
    """ใช้กับ WebSocket และ route ที่ต้องตรวจบทบาทหลังตรวจ cookie แล้ว."""

    if payload.get("role") != required_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ไม่มีสิทธิ์เข้าถึง",
        )

    return payload


def verify_pending_or_active_user(request: Request):
    """
    ตรวจ session สำหรับ 2 กรณี:

    1) ผู้ใช้ใหม่ที่กำลังกรอกฟอร์ม ProfileSetup
       -> token จะมี "registration": True และ "user_id" ยังไม่มีอยู่จริงใน DB
          (เป็นเรื่องปกติ เพราะยังไม่เคยถูกสร้าง) จึงต้องอนุญาตให้ผ่านได้เลย
          โดยไม่ไปเช็คกับ get_UserDB()   ← ★ จุดที่แก้บั๊ก 401

    2) ผู้ใช้เดิมที่มีข้อมูลใน DB แล้วแต่สถานะยังเป็น Pending
       -> ต้องมีข้อมูลอยู่จริงใน DB เท่านั้นถึงจะผ่าน
    """

    token = request.cookies.get(COOKIE_NAME)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="กรุณาเข้าสู่ระบบ",
        )

    try:
        payload = decode_token(token)

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token ไม่ถูกต้องหรือหมดอายุ",
        )

    user_id = payload.get("user_id")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token ไม่ถูกต้อง",
        )

    # ✅ กรณีที่ 1: registration token ของผู้ใช้ใหม่ -> ไม่ต้องเช็ค DB

    if payload.get("registration"):
        return payload

    # ✅ กรณีที่ 2: ผู้ใช้เดิม -> ต้องมีข้อมูลอยู่ใน DB จริง

    if not get_UserDB(user_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ไม่พบข้อมูลผู้ใช้",
        )

    return payload


# ============================================================
# LINE LOGIN
# ============================================================

@router.post("/Login")
@limit("5/minute")
def Line_Login(request: Request, data: LineAuthCode, response: Response):
    """
    Login ด้วย LINE OAuth 2.0
    """
    try:
        access_token = exchange_code_for_access_token(data.code)
        line_profile = verify_line_token(access_token)

        line_user_id = line_profile.get("userId")

        if not line_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ไม่สามารถดึง userId จาก LINE ได้",
            )

        line_picture_url = line_profile.get("pictureUrl", "")
        db_user = get_UserDB(line_user_id)

        # --------------------------------------------------------
        # ผู้ใช้ใหม่ -> ออก registration token แล้วส่งไปกรอกโปรไฟล์
        # --------------------------------------------------------

        if not db_user:
            registration_token = encode_token(
                {
                    "user_id": line_user_id,
                    "registration": True,
                },
                timedelta(minutes=15),
            )

            set_user_session(request, response, registration_token)

            return {
                "Role": None,
                "Status": None,
                "lineUserId": line_user_id,
                "pictureUrl": line_picture_url,
            }

        # --------------------------------------------------------
        # ผู้ใช้เดิมที่ถูกระงับ
        # --------------------------------------------------------

        if db_user.get("Status") in BLOCKED_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="บัญชีของคุณถูกระงับ กรุณาติดต่อผู้ดูแลระบบ",
            )

        # --------------------------------------------------------
        # อัปเดตรูปโปรไฟล์จาก LINE ถ้ายังไม่เคยตั้งรูปเอง
        # --------------------------------------------------------

        image_url = db_user.get("imageURL", "")

        is_line_or_empty = (
            image_url in ("", "line", None)
            or "profile.line-scdn.net" in (image_url or "")
        )

        if line_picture_url and is_line_or_empty:
            if image_url != line_picture_url:
                Connect_MongoDB()["BORC"]["UserProfile"].update_one(
                    {"userId": line_user_id},
                    {
                        "$set": {
                            "imageURL": line_picture_url,
                            "updatedAt": datetime.now(timezone.utc),
                        }
                    },
                )

            image_url = line_picture_url

        token_payload = {
            "user_id": db_user.get("userId", line_user_id),
            "role": db_user.get("Role"),
            "Prefix": db_user.get("Prefix", ""),
            "Firstname": db_user.get("Firstname", ""),
            "Lastname": db_user.get("Lastname", ""),
            "Status": db_user.get("Status"),
            "imageURL": image_url,
        }

        access_token_jwt = encode_token(token_payload, SESSION_MAX_AGE)

        set_user_session(request, response, access_token_jwt)

        return {
            "userId": db_user.get("userId", line_user_id),
            "Role": db_user.get("Role"),
            "Status": db_user.get("Status"),
            "Prefix": db_user.get("Prefix", ""),
            "Lastname": db_user.get("Lastname", ""),
            "Firstname": db_user.get("Firstname", ""),
            "ImageUrl": image_url,
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error in Line_Login")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
# LOGOUT
# ============================================================

@router.post("/Logout")
async def Logout(request: Request, response: Response):
    """
    Logout User
    ลบ JWT Cookie ออกจาก Browser
    """

    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        path="/",
        **_cookie_security_flags(request),
    )

    return {"message": "ออกจากระบบสำเร็จ"}


# ============================================================
# NAVBAR USER
# ============================================================

@router.get("/NavbarUsers")
def get_NavbarUsers(payload: dict = Depends(verify_user_token), response: Response = None):
    """
    ดึงข้อมูล User ปัจจุบันจาก MongoDB
    """

    db_user = get_UserDB(payload["user_id"])

    if not db_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ไม่พบข้อมูลผู้ใช้",
        )

    if response:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

    image_url = db_user.get("imageURL", "")
    updated_at = db_user.get("updatedAt")

    if image_url and updated_at and "profile.line-scdn.net" not in image_url and image_url != "line":
        timestamp = int(updated_at.timestamp()) if hasattr(updated_at, "timestamp") else ""

        if timestamp:
            image_url = f"{image_url}?t={timestamp}"

    return {
        "user_id": db_user["userId"],
        "role": db_user["Role"],
        "Prefix": db_user["Prefix"],
        "Firstname": db_user["Firstname"],
        "Lastname": db_user["Lastname"],
        "ImageUrl": image_url,
    }


# ============================================================
# STUDENT DASHBOARD
# ============================================================

@router.get("/Student/dashboard")
def Student_dashboard(payload: dict = Depends(verify_user_token)):
    """
    Dashboard สำหรับ Student
    (ใช้ข้อมูลที่ verify_user_token ตรวจแล้ว ไม่ต้องค้น UserProfile ซ้ำ)
    """

    if payload["role"] != "Student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="เฉพาะนักศึกษาเท่านั้น",
        )

    return {
        "message": f"{payload['Prefix']}{payload['Firstname']} {payload['Lastname']}",
        "role": payload["role"],
    }


# ============================================================
# ADVISOR DASHBOARD
# ============================================================

@router.get("/Advisor/dashboard")
def Advisor_dashboard(payload: dict = Depends(verify_user_token)):
    """
    Dashboard สำหรับ Advisor
    (ใช้ข้อมูลที่ verify_user_token ตรวจแล้ว ไม่ต้องค้น UserProfile ซ้ำ)
    """

    if payload["role"] != "Advisor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="เฉพาะอาจารย์เท่านั้น",
        )

    return {
        "message": f"{payload['Prefix']}{payload['Firstname']} {payload['Lastname']}",
        "role": payload["role"],
    }


# ============================================================
# CURRENT USER
# ============================================================

@router.get("/Me")
def get_Me(payload: dict = Depends(verify_user_token)):
    """
    ตรวจสอบว่า User ยัง Login อยู่หรือไม่
    (ใช้ข้อมูลที่ verify_user_token ตรวจแล้ว ไม่ต้องค้น UserProfile ซ้ำ)
    """

    return {
        "user_id": payload["user_id"],
        "role": payload["role"],
        "status": payload.get("Status", ""),
        "Prefix": payload.get("Prefix", ""),
        "Firstname": payload.get("Firstname", ""),
        "Lastname": payload.get("Lastname", ""),
        "ImageUrl": payload.get("imageURL", ""),
    }

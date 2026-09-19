"""
common/jwt_utils.py

รวม logic การเข้ารหัส/ถอดรหัส JWT ที่ใช้ร่วมกันระหว่าง
Admin/auth/authAdmin.py และ users/auth/authUser.py

เดิมทั้งสองไฟล์ต่าง implement encode/decode JWT ของตัวเอง (โค้ดซ้ำกัน)
โมดูลนี้รวม logic กลางไว้ที่เดียว ส่วน cookie handling, role ตรวจสอบ,
และ business logic เฉพาะของแต่ละฝั่ง (Admin / User) ยังคงอยู่ในไฟล์เดิม
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import jwt
from jwt import PyJWTError as JWTError

load_dotenv()

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"


def _get_secret_key() -> str:
    """ดึง JWT_SECRET_KEY จาก environment ทุกครั้งที่เรียกใช้

    (ไม่ cache เป็น module-level constant เพื่อให้ทดสอบ/override
    ค่าผ่าน os.environ ได้ง่าย เช่นในชุดทดสอบ)
    """
    secret = os.getenv("JWT_SECRET_KEY")
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY is not set in environment variables")
    return secret


def encode_token(data: dict, expires_delta: timedelta) -> str:
    """สร้าง JWT token จาก payload dict พร้อมกำหนดวันหมดอายุ

    Args:
        data: payload ที่ต้องการเข้ารหัส (เช่น sub, user_id, role)
        expires_delta: ระยะเวลาก่อนหมดอายุ (timedelta)
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, _get_secret_key(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """ถอดรหัส JWT token คืนค่า payload dict

    Raises:
        JWTError: ถ้า token ไม่ถูกต้องหรือหมดอายุ (คือ jwt.PyJWTError ครอบคลุมทุกชนิดของข้อผิดพลาด)
    """
    # verify_iat=False: คงพฤติกรรมเดิมของ python-jose (ไม่ปฏิเสธ token ที่ iat ล้ำหน้านาฬิกาของเครื่องนี้)
    # กัน instance ที่นาฬิกาเร็ว/ช้ากว่ากันปฏิเสธ token ของกันและกัน; exp ยังตรวจตามปกติ
    # algorithms ระบุ HS256 เท่านั้น จึงปฏิเสธ alg=none และอัลกอริทึมอื่น
    return jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM], options={"verify_iat": False})


__all__ = ["encode_token", "decode_token", "ALGORITHM", "JWTError"]

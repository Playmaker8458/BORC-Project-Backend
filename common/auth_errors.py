"""
common/auth_errors.py

ตัวจัดการ 401 กลางของทั้งระบบ (ฝั่ง backend)

ปัญหาที่แก้: เมื่อ session ของผู้ใช้ครบ 24 ชั่วโมง (หรือ token เสีย) ทุก endpoint ที่ใช้
verify_user_token จะตอบ 401 แต่ cookie เสียใบเดิมยังค้างอยู่ในเบราว์เซอร์และถูกส่งกลับมาซ้ำ ๆ
ตัวจัดการนี้ทำให้ "ทุก" 401 ที่เกิดจาก session cookie ของผู้ใช้ที่หมดอายุ/ใช้ไม่ได้
พ่วงคำสั่งลบ cookie ไปด้วย เบราว์เซอร์จึงออกจากระบบทันที และ frontend ที่ได้ 401 พาไปหน้า login ได้

หลักการที่ตั้งใจไว้ (เพื่อไม่ให้พฤติกรรมเดิมเปลี่ยน):
  - ทำงานเฉพาะ status 401 เท่านั้น — response อื่น (403, 404, 409, 422, 500 ...) ไม่ถูกแตะ
  - body / status / header เดิมของ response ยังมาจาก handler มาตรฐานของ FastAPI เหมือนเดิมทุกอย่าง
  - ลบ cookie เฉพาะเมื่อ request แนบ session cookie ของผู้ใช้มา "และ" token ในนั้นถอดรหัสไม่ผ่าน
    (หมดอายุ/ลายเซ็นเสีย/ไม่มี exp) — 401 จากสาเหตุอื่น เช่น ไม่มี cookie, LINE code ผิด,
    หรือ 401 ของฝั่งแอดมิน จะไม่ไปลบ session ที่ยังใช้ได้ของผู้ใช้
  - ไม่แตะ cookie ของแอดมิน (คนละชื่อ) ไม่ว่ากรณีใด
  - ถ้า response มี Set-Cookie ลบ session อยู่แล้ว (เช่นจาก authUser._clear_session_headers)
    จะไม่เพิ่มซ้ำ
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.cookies import cookie_security_flags
from common.jwt_utils import JWTError, decode_token

logger = logging.getLogger(__name__)

# ต้องตรงกับ COOKIE_NAME ใน users/auth/authUser.py
USER_COOKIE_NAME = "access_token"


def _user_session_cookie_is_dead(request: Request) -> bool:
    """True เมื่อ request แนบ session cookie ของผู้ใช้มา แต่ token ข้างในใช้ไม่ได้แล้ว"""

    token = request.cookies.get(USER_COOKIE_NAME)
    if not token:
        return False

    try:
        decode_token(token)
    except JWTError:
        return True
    except Exception:  # noqa: BLE001
        # เช่น JWT_SECRET_KEY หายจาก environment — ไม่ใช่ความผิดของ cookie ผู้ใช้
        # จึงไม่ลบ session ของใครทั้งสิ้น
        logger.exception("ตรวจ session cookie ใน 401 handler ไม่สำเร็จ")
        return False

    return False


def _already_clears_session_cookie(response) -> bool:
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie" and value.startswith(f"{USER_COOKIE_NAME}=".encode()):
            return True
    return False


async def unauthorized_cookie_cleanup_handler(request: Request, exc: StarletteHTTPException):
    """handler มาตรฐานของ FastAPI + ลบ session cookie ที่ใช้ไม่ได้แล้วเมื่อตอบ 401"""

    response = await http_exception_handler(request, exc)

    if (
        exc.status_code == 401
        and _user_session_cookie_is_dead(request)
        and not _already_clears_session_cookie(response)
    ):
        response.delete_cookie(
            key=USER_COOKIE_NAME,
            httponly=True,
            path="/",
            **cookie_security_flags(request),
        )

    return response


def register_auth_error_handlers(app: FastAPI) -> None:
    """ลงทะเบียนตัวจัดการ 401 กลางให้แอป (เรียกครั้งเดียวตอนสร้าง app)

    หมายเหตุ: ใช้ตัวจัดการเดียวกับที่ FastAPI ใช้เป็นค่าเริ่มต้นของ HTTPException
    ถ้าแอปมี exception handler ของ HTTPException ของตัวเองอยู่แล้ว ให้เรียกฟังก์ชัน
    unauthorized_cookie_cleanup_handler จากใน handler นั้นแทนการลงทะเบียนซ้ำ
    """

    app.add_exception_handler(StarletteHTTPException, unauthorized_cookie_cleanup_handler)


__all__ = [
    "USER_COOKIE_NAME",
    "unauthorized_cookie_cleanup_handler",
    "register_auth_error_handlers",
]
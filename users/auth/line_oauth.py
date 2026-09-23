"""แลก LINE OAuth Authorization Code เป็น Access Token และดึง Profile

แยกออกจาก authUser.py (เดิมทั้ง config + LINE HTTP calls + JWT/session +
route handlers อยู่รวมกันในไฟล์เดียว 674 บรรทัด) — ส่วนนี้เป็น LINE API
client ล้วนๆ ไม่ผูกกับ DB/JWT จึงแยกออกมาได้โดยไม่กระทบจุดอื่น
"""

import logging
import os

import requests
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


# ============================================================
# LINE Login Configuration
# ============================================================

LINE_LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID")
LINE_LOGIN_CHANNEL_SECRET = os.getenv("LINE_LOGIN_CHANNEL_SECRET")
LINE_LOGIN_REDIRECT_URI = os.getenv("LINE_LOGIN_REDIRECT_URI")

if not LINE_LOGIN_CHANNEL_ID or not LINE_LOGIN_CHANNEL_SECRET or not LINE_LOGIN_REDIRECT_URI:
    raise RuntimeError(
        "LINE_LOGIN_CHANNEL_ID / LINE_LOGIN_CHANNEL_SECRET / LINE_LOGIN_REDIRECT_URI ยังไม่ได้ตั้งค่าใน .env"
    )


# ============================================================
# LINE OAuth
# ============================================================

def exchange_code_for_access_token(code: str) -> str:
    """
    แลก Authorization Code จาก LINE
    เป็น Access Token

    ขั้นตอนนี้ต้องทำที่ Backend เท่านั้น
    เนื่องจากต้องใช้ LINE Channel Secret
    """

    try:
        response = requests.post(
            "https://api.line.me/oauth2/v2.1/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": LINE_LOGIN_REDIRECT_URI,
                "client_id": LINE_LOGIN_CHANNEL_ID,
                "client_secret": LINE_LOGIN_CHANNEL_SECRET,
            },
            timeout=30,
        )

        if response.status_code != 200:
            # เดิมไม่ log อะไรเลยตรงนี้ (log เฉพาะ RequestException ด้านล่าง) ทำให้ไม่มีทาง
            # รู้สาเหตุจริงที่ LINE ปฏิเสธ (เช่น redirect_uri ไม่ตรงกับที่ลงทะเบียนไว้ใน
            # LINE Developers Console, code ถูกใช้ไปแล้ว/หมดอายุ, client_secret ผิด)
            logger.warning(
                "[LINE] Token exchange failed: status=%s body=%s",
                response.status_code, response.text,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization Code ไม่ถูกต้อง หรือหมดอายุ กรุณาลองเข้าสู่ระบบใหม่อีกครั้ง",
            )

        token_data = response.json()

        access_token = token_data.get("access_token")

        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="ไม่สามารถรับ Access Token จาก LINE ได้",
            )

        return access_token

    except requests.exceptions.RequestException as exc:
        logger.warning("[LINE] Token request failed: %s", repr(exc))

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ไม่สามารถเชื่อมต่อระบบ LINE ได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง",
        )


# ============================================================
# LINE Profile
# ============================================================

def verify_line_token(access_token: str):
    """
    ตรวจสอบ Access Token
    และดึงข้อมูล Profile จาก LINE
    """

    try:
        response = requests.get(
            "https://api.line.me/v2/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="AccessToken ไม่ถูกต้อง หรือหมดอายุ",
            )

        return response.json()

    except requests.exceptions.RequestException as exc:
        logger.warning("[LINE] Profile request failed: %s", repr(exc))

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ไม่สามารถเชื่อมต่อระบบ LINE ได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง",
        )

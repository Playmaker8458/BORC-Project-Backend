"""
common/rate_limit.py

Limiter instance เดียวที่ใช้ร่วมกันระหว่าง Admin/auth/authAdmin.py และ
users/auth/authUser.py (endpoint login / OAuth code-exchange เท่านั้น
ไม่ได้จำกัด rate ทั้งระบบ) รวมถึง main.py ที่ผูก exception handler

ถ้ายังไม่ได้ติดตั้ง slowapi (เช่นในบาง environment สำหรับรัน unit test)
`limiter` จะเป็น None และ endpoint จะไม่ถูกจำกัด rate แทนที่จะ error
"""

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address)
except ImportError:  # pragma: no cover - slowapi ไม่ได้ติดตั้ง
    limiter = None


def limit(rate: str):
    """decorator factory ที่ใช้ได้แม้ไม่ได้ติดตั้ง slowapi (จะกลาย noop)"""
    if limiter is None:
        return lambda f: f
    return limiter.limit(rate)

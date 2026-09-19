"""
common/rate_limit.py

Limiter instance เดียวที่ใช้ร่วมกันระหว่าง Admin/auth/authAdmin.py และ
users/auth/authUser.py (endpoint login / OAuth code-exchange เท่านั้น
ไม่ได้จำกัด rate ทั้งระบบ) รวมถึง main.py ที่ผูก exception handler

ถ้ายังไม่ได้ติดตั้ง slowapi (เช่นในบาง environment สำหรับรัน unit test)
`limiter` จะเป็น None และ endpoint จะไม่ถูกจำกัด rate แทนที่จะ error
"""

import ipaddress
import os

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
except ImportError:  # pragma: no cover - slowapi ไม่ได้ติดตั้ง
    Limiter = None


def client_ip(request) -> str:
    """IP ของผู้ใช้จริง ใช้เป็นกุญแจนับ rate limit

    หลัง proxy ของ Railway ทุกคำขอมาจาก IP ของ proxy (request.client.host) ทำให้ผู้ใช้ทุกคนใช้
    โควตาเดียวกัน (ผู้โจมตี 1 คนล็อกอินของทุกคนได้) Railway ใส่ IP ผู้ใช้จริงไว้ใน header X-Real-IP
    (docs.railway.com/networking/public-networking/specs-and-limits) จึงใช้ค่านี้เมื่อ ENV=production
    เท่านั้น — นอก production ไม่มี proxy ที่น่าเชื่อถือ ผู้ใช้ปลอม header เองได้

    ไม่ใช้ X-Forwarded-For: ค่าซ้ายสุดผู้ใช้ส่งมาเองได้ เปลี่ยนไปเรื่อย ๆ เพื่อหลบ limit ได้
    ถ้า header ว่างหรือไม่ใช่ IP ที่ถูกต้อง ให้ย้อนกลับไปใช้ IP ของ peer
    """
    if os.getenv("ENV") == "production":
        candidate = (request.headers.get("x-real-ip") or "").strip()
        if candidate and len(candidate) <= 45:
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip) if Limiter is not None else None


def limit(rate: str):
    """decorator factory ที่ใช้ได้แม้ไม่ได้ติดตั้ง slowapi (จะกลาย noop)"""
    if limiter is None:
        return lambda f: f
    return limiter.limit(rate)

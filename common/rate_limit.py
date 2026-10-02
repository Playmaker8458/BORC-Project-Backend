"""
common/rate_limit.py

Limiter instance เดียวที่ใช้ร่วมกันระหว่าง Admin/auth/authAdmin.py และ
users/auth/authUser.py (endpoint login / OAuth code-exchange เท่านั้น
ไม่ได้จำกัด rate ทั้งระบบ) รวมถึง main.py ที่ผูก exception handler

ถ้ายังไม่ได้ติดตั้ง slowapi (เช่นในบาง environment สำหรับรัน unit test)
`limiter` จะเป็น None และ endpoint จะไม่ถูกจำกัด rate แทนที่จะ error
"""

import hmac
import ipaddress
import os

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
except ImportError:  # pragma: no cover - slowapi ไม่ได้ติดตั้ง
    Limiter = None


def _valid_ip(value: str) -> str | None:
    value = (value or "").strip()
    if not value or len(value) > 45:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _ip_from_trusted_proxy(request) -> str | None:
    """IP ผู้ใช้จริงเมื่อคำขอมาผ่าน proxy ของ Vercel (/api/* rewrite ใน vercel.json)

    ผ่าน proxy นั้น X-Real-IP ของ Railway จะเป็น IP ของ Vercel ทุกคำขอ ทำให้ทุกคนใช้โควตาเดียวกัน
    Vercel จึงแนบ header ลับ X-Proxy-Secret (ค่า API_PROXY_SECRET เดียวกับที่ตั้งไว้ใน env ของ backend)
    ถ้าตรงกัน แปลว่าคำขอผ่าน Vercel จริง และ Vercel เขียนทับ X-Forwarded-For ด้วย IP ผู้ใช้จริงเอง
    (ผู้ใช้ปลอมไม่ได้) จึงใช้ค่าซ้ายสุดของ header นั้น — ถ้าไม่ได้ตั้ง secret หรือไม่ตรง จะไม่เชื่อ header ใดเลย
    """
    expected = os.getenv("API_PROXY_SECRET") or ""
    provided = request.headers.get("x-proxy-secret") or ""
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        return None
    forwarded = request.headers.get("x-forwarded-for") or ""
    return _valid_ip(forwarded.split(",")[0])


def client_ip(request) -> str:
    """IP ของผู้ใช้จริง ใช้เป็นกุญแจนับ rate limit

    หลัง proxy ของ Railway ทุกคำขอมาจาก IP ของ proxy (request.client.host) ทำให้ผู้ใช้ทุกคนใช้
    โควตาเดียวกัน (ผู้โจมตี 1 คนล็อกอินของทุกคนได้) Railway ใส่ IP ผู้ใช้จริงไว้ใน header X-Real-IP
    (docs.railway.com/networking/public-networking/specs-and-limits) จึงใช้ค่านี้เมื่อ ENV=production
    เท่านั้น — นอก production ไม่มี proxy ที่น่าเชื่อถือ ผู้ใช้ปลอม header เองได้

    ไม่ใช้ X-Forwarded-For: ค่าซ้ายสุดผู้ใช้ส่งมาเองได้ เปลี่ยนไปเรื่อย ๆ เพื่อหลบ limit ได้
    ถ้า header ว่างหรือไม่ใช่ IP ที่ถูกต้อง ให้ย้อนกลับไปใช้ IP ของ peer
    """
    return resolve_client_ip(request)[0]


def resolve_client_ip(request) -> tuple[str, str]:
    """(IP, แหล่งที่มา) — แหล่งเป็น "proxy-x-forwarded-for" | "x-real-ip" | "peer" """
    if os.getenv("ENV") == "production":
        proxied = _ip_from_trusted_proxy(request)
        if proxied:
            return proxied, "proxy-x-forwarded-for"
        real = _valid_ip(request.headers.get("x-real-ip") or "")
        if real:
            return real, "x-real-ip"
    return get_remote_address(request), "peer"


def proxy_diagnostics(request) -> dict:
    """สถานะล้วนๆ สำหรับหาสาเหตุที่ rate limit ไม่แยกผู้ใช้ — ห้ามมีค่า secret หรือ IP ใดๆ ในผลลัพธ์"""
    expected = os.getenv("API_PROXY_SECRET") or ""
    provided = request.headers.get("x-proxy-secret") or ""
    forwarded = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    first = _valid_ip(forwarded[0]) if forwarded else None
    real = _valid_ip(request.headers.get("x-real-ip") or "")
    return {
        "env_production": os.getenv("ENV") == "production",
        "proxy_secret_configured": bool(expected),
        "proxy_secret_header_present": bool(provided),
        "proxy_secret_matches": bool(expected) and hmac.compare_digest(provided.encode(), expected.encode()),
        "xff_entries": len(forwarded),
        "xff_first_is_valid_ip": first is not None,
        "xff_first_equals_x_real_ip": first is not None and first == real,
        "client_ip_source": resolve_client_ip(request)[1],
        "xff_distinct_entries": len(set(forwarded)),
        "vercel_forwarded_for_is_valid_ip": _valid_ip(request.headers.get("x-vercel-forwarded-for") or "") is not None,
        "forwarding_headers": sorted(k for k in request.headers.keys()
                                     if k.startswith(("x-vercel", "x-forwarded", "x-real", "x-envoy", "forwarded", "cf-"))),
    }


limiter = Limiter(key_func=client_ip) if Limiter is not None else None


def limit(rate: str):
    """decorator factory ที่ใช้ได้แม้ไม่ได้ติดตั้ง slowapi (จะกลาย noop)"""
    if limiter is None:
        return lambda f: f
    return limiter.limit(rate)

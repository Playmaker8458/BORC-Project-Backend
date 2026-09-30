"""flag ความปลอดภัยของคุกกี้ session (ใช้ร่วมกันระหว่างคุกกี้ผู้ใช้และคุกกี้แอดมิน)"""

from fastapi import Request


def cookie_security_flags(request: Request) -> dict:
    """secure/samesite ตามว่าคำขอมาทาง HTTPS หรือไม่

    HTTPS (รวมหลัง proxy ที่ส่ง X-Forwarded-Proto): secure=True, samesite="none" (frontend/backend คนละโดเมน)
    localhost (HTTP): secure=False, samesite="lax" (SameSite=None ใช้ได้เฉพาะบน HTTPS — ข้อบังคับของเบราว์เซอร์)
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = request.url.scheme == "https" or forwarded_proto.lower() == "https"
    return {"secure": is_https, "samesite": "none" if is_https else "lax"}

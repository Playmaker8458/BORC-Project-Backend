"""ตรวจ Origin ของคำขอที่เปลี่ยนข้อมูลและ WebSocket (กัน CSRF / Cross-site WebSocket Hijacking)

ที่มา: cookie ของผู้ใช้เป็น SameSite=None (frontend บน Vercel กับ backend บน Railway คนละไซต์)
เว็บอื่นจึงสั่งให้เบราว์เซอร์ของเหยื่อยิง POST แบบฟอร์ม หรือเปิด WebSocket ไปที่ backend พร้อม cookie ได้
CORS กันได้แค่ "การอ่านผลลัพธ์" ไม่ได้กัน "การส่งคำขอ" เซิร์ฟเวอร์จึงต้องตรวจ Origin เอง

กฎ:
- POST/PUT/PATCH/DELETE และ WebSocket ที่มี header Origin ซึ่งไม่อยู่ในรายชื่อ -> ปฏิเสธ
  (HTTP 403 / WebSocket ปิดด้วย 1008)
- ไม่มี Origin -> ผ่าน: เบราว์เซอร์ใส่ Origin ให้เสมอในคำขอข้ามไซต์ ส่วน curl/server-to-server
  (เช่น ChatBot) ไม่ใช่ช่องทางของ CSRF
- GET/HEAD/OPTIONS ไม่ตรวจ (ไม่เปลี่ยนข้อมูล และ OPTIONS เป็น preflight ของ CORS)
- รายชื่อว่าง -> ไม่บังคับ (กัน config ผิดแล้วบล็อกการเขียนทั้งระบบ)

สวิตช์ฉุกเฉินผ่าน env ORIGIN_CHECK (อ่านทุกคำขอ):
  enforce (ค่าเริ่มต้น / ค่าที่ไม่รู้จัก) = บล็อก | log = บันทึกอย่างเดียวไม่บล็อก | off = ปิด
"""

import logging
import os

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MODES = {"enforce", "log", "off"}
_WS_POLICY_VIOLATION = 1008


def _mode() -> str:
    mode = (os.getenv("ORIGIN_CHECK") or "enforce").strip().lower()
    return mode if mode in _MODES else "enforce"


def _normalize(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class OriginGuardMiddleware:
    """ASGI middleware ตรวจ Origin — ต้อง add ก่อน CORSMiddleware เพื่อให้ CORS อยู่ชั้นนอกสุด"""

    def __init__(self, app, allowed_origins):
        self.app = app
        self.allowed = {_normalize(o) for o in allowed_origins if o and o.strip()}
        if not self.allowed:
            logger.warning("OriginGuard: ไม่มีรายชื่อ Origin ที่อนุญาต จึงไม่ตรวจ Origin (ตรวจ Frontend_BORC_URL)")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not self.allowed:
            return await self.app(scope, receive, send)

        mode = _mode()
        if mode == "off":
            return await self.app(scope, receive, send)

        if scope["type"] == "http" and scope["method"] not in _WRITE_METHODS:
            return await self.app(scope, receive, send)

        origin = None
        for name, value in scope.get("headers", []):
            if name == b"origin":
                origin = value.decode("latin-1")
                break

        if origin is None or _normalize(origin) in self.allowed:
            return await self.app(scope, receive, send)

        logger.warning(
            "OriginGuard[%s]: Origin ไม่ได้รับอนุญาต origin=%r type=%s method=%s path=%s",
            mode, origin[:200], scope["type"], scope.get("method", "WS"), scope.get("path"),
        )
        if mode == "log":
            return await self.app(scope, receive, send)

        if scope["type"] == "http":
            response = JSONResponse({"detail": "Origin ไม่ได้รับอนุญาต"}, status_code=403)
            return await response(scope, receive, send)

        await receive()  # websocket.connect — ต้องรับก่อนจึงปิดการเชื่อมต่อได้ตามข้อกำหนด ASGI
        await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})

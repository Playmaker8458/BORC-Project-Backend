"""Security headers ของ API และการปิด /docs บน production

- SecurityHeadersMiddleware เติม header ป้องกันให้ทุก HTTP response (รวม 4xx/5xx และ preflight)
  โดย **ไม่ทับ** header ที่ endpoint ตั้งเอง (เช่น Cache-Control ของ /NavbarUsers)
- docs_kwargs ปิด /docs /redoc /openapi.json เมื่อ ENV=production (ไม่งั้นเผยรายการ endpoint
  ทั้งหมดรวมของแอดมินให้ทุกคน) เปิดกลับได้ด้วย env ENABLE_DOCS=true เมื่อจำเป็นต้องดีบัก
"""

_TRUTHY = {"1", "true", "yes", "on"}
_HSTS = "max-age=31536000"  # 1 ปี; ไม่ใส่ includeSubDomains/preload เพื่อลดผลกระทบถ้าตั้งค่าผิด


def docs_kwargs(is_prod: bool, enable_docs: str | None) -> dict:
    """คืนอาร์กิวเมนต์ที่ส่งเข้า FastAPI(...) เพื่อปิดหน้า docs บน production"""
    if is_prod and (enable_docs or "").strip().lower() not in _TRUTHY:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


class SecurityHeadersMiddleware:
    """ASGI middleware — เติม header เฉพาะ HTTP (WebSocket ไม่แตะ)"""

    def __init__(self, app, hsts: bool = False, csp: str | None = None):
        self.app = app
        defaults = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        ]
        if hsts:
            defaults.append((b"strict-transport-security", _HSTS.encode()))
        if csp:
            defaults.append((b"content-security-policy", csp.encode()))
        self._defaults = defaults

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((n, v) for n, v in self._defaults if n not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

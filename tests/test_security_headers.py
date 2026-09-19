"""
Step 4 ของการเสริมความปลอดภัย: ปิด /docs บน production และเพิ่ม security headers ให้ API

- /docs, /redoc, /openapi.json เปิดสาธารณะบน production (พิสูจน์แล้วทั้งในเครื่องและ Railway จริง)
  เผยรายการ endpoint ทั้งหมดรวมของแอดมิน -> ปิดเมื่อ ENV=production เว้นแต่ตั้ง ENABLE_DOCS=true
- API ตอบโดยไม่มี header ป้องกัน (nosniff, frame, referrer, HSTS ฯลฯ)
"""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from common.security_headers import SecurityHeadersMiddleware, docs_kwargs

MAIN_SRC = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")


# ── docs_kwargs ──────────────────────────────────────────────────────────────

def test_docs_disabled_in_production_by_default():
    assert docs_kwargs(is_prod=True, enable_docs="") == {"docs_url": None, "redoc_url": None, "openapi_url": None}


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", " on "])
def test_enable_docs_flag_reenables_docs_in_production(flag):
    assert docs_kwargs(is_prod=True, enable_docs=flag) == {}


@pytest.mark.parametrize("flag", ["0", "false", "no", "banana", None])
def test_other_flag_values_keep_docs_disabled_in_production(flag):
    assert docs_kwargs(is_prod=True, enable_docs=flag)["docs_url"] is None


def test_docs_enabled_outside_production():
    assert docs_kwargs(is_prod=False, enable_docs="") == {}


def test_prod_app_has_no_docs_routes_and_dev_app_does():
    prod = TestClient(FastAPI(**docs_kwargs(is_prod=True, enable_docs="")))
    dev = TestClient(FastAPI(**docs_kwargs(is_prod=False, enable_docs="")))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert prod.get(path).status_code == 404, path
        assert dev.get(path).status_code == 200, path


# ── security headers ─────────────────────────────────────────────────────────

def _app(**mw):
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=403, detail="no")

    @app.get("/own-headers")
    def own():
        return JSONResponse({"ok": True}, headers={"X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"hi": 1})
        await websocket.close()

    app.add_middleware(SecurityHeadersMiddleware, **mw)
    return app


def test_baseline_headers_on_success_and_error_responses():
    c = TestClient(_app(hsts=False, csp=None))
    for path in ("/ok", "/boom", "/missing"):
        h = c.get(path).headers
        assert h["x-content-type-options"] == "nosniff", path
        assert h["x-frame-options"] == "DENY", path
        assert h["referrer-policy"] == "no-referrer", path
        assert "camera=()" in h["permissions-policy"], path


def test_hsts_only_when_enabled():
    assert "strict-transport-security" not in TestClient(_app(hsts=False, csp=None)).get("/ok").headers
    hsts = TestClient(_app(hsts=True, csp=None)).get("/ok").headers["strict-transport-security"]
    assert re.match(r"max-age=\d{7,}", hsts) and "preload" not in hsts


def test_csp_only_when_configured():
    assert "content-security-policy" not in TestClient(_app(hsts=False, csp=None)).get("/ok").headers
    policy = "default-src 'none'; frame-ancestors 'none'"
    assert TestClient(_app(hsts=False, csp=policy)).get("/ok").headers["content-security-policy"] == policy


def test_existing_headers_from_the_app_are_not_overridden():
    """เช่น NavbarUsers ตั้ง Cache-Control เอง / endpoint ตั้ง header ของตัวเอง"""
    h = TestClient(_app(hsts=False, csp=None)).get("/own-headers").headers
    assert h["cache-control"] == "no-store"
    assert h["x-frame-options"] == "SAMEORIGIN"


def test_websocket_is_untouched():
    with TestClient(_app(hsts=True, csp="default-src 'none'")).websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"hi": 1}


# ── wiring ใน main.py ───────────────────────────────────────────────────────

def test_main_uses_docs_kwargs_and_security_headers():
    assert "docs_kwargs(" in MAIN_SRC
    assert re.search(r"FastAPI\(lifespan=lifespan,\s*\*\*_DOCS\)", MAIN_SRC)
    assert re.search(r"app\.add_middleware\(\s*SecurityHeadersMiddleware", MAIN_SRC)


def test_security_headers_wrap_cors_so_preflight_and_403_also_get_headers():
    """add_middleware ทีหลัง = ชั้นนอกกว่า -> ต้องมาหลัง CORS"""
    cors = re.search(r"app\.add_middleware\(\s*CORSMiddleware", MAIN_SRC)
    sec = re.search(r"app\.add_middleware\(\s*SecurityHeadersMiddleware", MAIN_SRC)
    assert cors and sec and sec.start() > cors.start()

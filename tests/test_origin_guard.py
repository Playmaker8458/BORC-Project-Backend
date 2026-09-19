"""
Step 2 ของการเสริมความปลอดภัย: ตรวจ Origin ของคำขอที่เปลี่ยนข้อมูลและ WebSocket

ปัญหาเดิม (พิสูจน์ได้จากการเจาะระบบในเครื่อง): cookie ของผู้ใช้เป็น SameSite=None (frontend/backend คนละไซต์)
เว็บอื่นจึงสั่งให้เบราว์เซอร์ของเหยื่อยิง POST แบบฟอร์ม/เปิด WebSocket มาที่ backend พร้อม cookie ได้
เซิร์ฟเวอร์ไม่เคยตรวจ Origin

กฎ:
- POST/PUT/PATCH/DELETE และ WebSocket ที่มี Origin ซึ่งไม่อยู่ในรายชื่อ (ชุดเดียวกับ CORS) -> ปฏิเสธ
- ไม่มี Origin (curl, server-to-server) -> ผ่าน (CSRF เกิดจากเบราว์เซอร์เท่านั้น ซึ่งใส่ Origin ให้เสมอ)
- GET/HEAD/OPTIONS ไม่ตรวจ
- ORIGIN_CHECK=enforce (ค่าเริ่มต้น) | log (บันทึกแต่ไม่บล็อก) | off (สวิตช์ฉุกเฉิน)
- รายชื่อว่าง -> ไม่บังคับ (กัน config ผิดพลาดแล้วบล็อกทั้งระบบ)
"""

import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from common.origin_guard import OriginGuardMiddleware

FRONT = "https://borc-project.vercel.app"
EVIL = {"Origin": "https://evil.example"}
GOOD = {"Origin": FRONT}


def _app(allowed=(FRONT, "http://localhost:5173")):
    app = FastAPI()

    @app.get("/r")
    def r_get():
        return {"ok": True}

    @app.post("/r")
    def r_post():
        return {"ok": True}

    @app.put("/r")
    def r_put():
        return {"ok": True}

    @app.patch("/r")
    def r_patch():
        return {"ok": True}

    @app.delete("/r")
    def r_delete():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"hello": "world"})
        await websocket.close()

    app.add_middleware(OriginGuardMiddleware, allowed_origins=list(allowed))
    return app


@pytest.fixture
def client():
    return TestClient(_app())


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_from_foreign_origin_are_blocked(client, method):
    resp = client.request(method, "/r", headers=EVIL)
    assert resp.status_code == 403
    assert "Origin" in resp.json()["detail"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_from_allowed_origin_pass(client, method):
    assert client.request(method, "/r", headers=GOOD).status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_without_origin_pass(client, method):
    """curl / server-to-server (เช่น ChatBot) ไม่มี Origin -> ต้องไม่ถูกบล็อก"""
    assert client.request(method, "/r").status_code == 200


def test_get_from_foreign_origin_is_not_checked(client):
    assert client.get("/r", headers=EVIL).status_code == 200


def test_options_is_not_checked(client):
    assert client.options("/r", headers=EVIL).status_code in (200, 405)


def test_origin_null_is_blocked_for_writes(client):
    assert client.post("/r", headers={"Origin": "null"}).status_code == 403


def test_origin_match_is_case_insensitive_and_ignores_trailing_slash_in_config():
    app = _app(allowed=["HTTPS://Borc-Project.Vercel.App/"])
    c = TestClient(app)
    assert c.post("/r", headers={"Origin": FRONT}).status_code == 200


def test_lookalike_origins_are_blocked(client):
    for origin in [FRONT + ".evil.example", "http://borc-project.vercel.app", "https://borc-project.vercel.app:8443",
                   "https://xborc-project.vercel.app"]:
        assert client.post("/r", headers={"Origin": origin}).status_code == 403, origin


def test_localhost_dev_origin_allowed_when_configured(client):
    assert client.post("/r", headers={"Origin": "http://localhost:5173"}).status_code == 200


# ── โหมดและ config ───────────────────────────────────────────────────────────

def test_log_mode_lets_request_through_but_logs(monkeypatch, caplog):
    monkeypatch.setenv("ORIGIN_CHECK", "log")
    c = TestClient(_app())
    with caplog.at_level(logging.WARNING):
        resp = c.post("/r", headers=EVIL)
    assert resp.status_code == 200
    assert any("evil.example" in r.getMessage() for r in caplog.records)


def test_off_mode_disables_the_check(monkeypatch):
    monkeypatch.setenv("ORIGIN_CHECK", "off")
    assert TestClient(_app()).post("/r", headers=EVIL).status_code == 200


def test_enforce_mode_logs_blocked_requests(monkeypatch, caplog):
    monkeypatch.setenv("ORIGIN_CHECK", "enforce")
    with caplog.at_level(logging.WARNING):
        assert TestClient(_app()).post("/r", headers=EVIL).status_code == 403
    assert any("evil.example" in r.getMessage() and "/r" in r.getMessage() for r in caplog.records)


def test_unknown_mode_falls_back_to_enforce(monkeypatch):
    monkeypatch.setenv("ORIGIN_CHECK", "banana")
    assert TestClient(_app()).post("/r", headers=EVIL).status_code == 403


def test_empty_allowed_list_does_not_enforce():
    """กัน config ผิด (ลืมตั้ง Frontend_BORC_URL) แล้วบล็อกการเขียนทั้งระบบ"""
    assert TestClient(_app(allowed=[])).post("/r", headers=EVIL).status_code == 200


# ── WebSocket ────────────────────────────────────────────────────────────────

def test_websocket_from_foreign_origin_is_rejected(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers=EVIL):
            pass
    assert exc.value.code == 1008


def test_websocket_from_allowed_origin_connects(client):
    with client.websocket_connect("/ws", headers=GOOD) as ws:
        assert ws.receive_json() == {"hello": "world"}


def test_websocket_without_origin_connects(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"hello": "world"}


def test_websocket_log_mode_connects_but_logs(monkeypatch, caplog):
    monkeypatch.setenv("ORIGIN_CHECK", "log")
    c = TestClient(_app())
    with caplog.at_level(logging.WARNING):
        with c.websocket_connect("/ws", headers=EVIL) as ws:
            assert ws.receive_json() == {"hello": "world"}
    assert any("evil.example" in r.getMessage() for r in caplog.records)


# ── wiring ใน main.py ───────────────────────────────────────────────────────

def test_main_registers_guard_inside_cors_with_same_origin_list():
    """CORS ต้องอยู่ชั้นนอกสุด (add_middleware ทีหลัง) เพื่อให้ preflight ถูกตอบโดย CORS
    และ response 403 ของ guard ไม่ได้ header allow-origin สำหรับ origin แปลกปลอม"""
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    guard = re.search(r"app\.add_middleware\(\s*OriginGuardMiddleware,\s*allowed_origins=origins", src)
    cors = re.search(r"app\.add_middleware\(\s*CORSMiddleware,\s*allow_origins=origins", src)
    assert guard and cors
    assert guard.start() < cors.start()

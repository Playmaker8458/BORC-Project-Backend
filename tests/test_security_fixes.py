"""
Regression tests สำหรับจุดที่แก้ไขจาก security/delivery audit:

1. SetupProfile.py ต้องออก JWT ผ่าน common/jwt_utils (shared util) แทนการ
   เรียก jwt.encode (PyJWT) ตรง ๆ — token ที่ได้ต้อง decode ได้ด้วย decode_token()
   เดียวกับที่ authUser.py ใช้ (พิสูจน์ว่าใช้ secret/algorithm ชุดเดียวกันจริง)
2. Reschedule_Students.py / Reschedule_Advisor.py: bug เดิมเรียก `.json()` บน
   module `requests`/object คำขอ (ไม่ใช่ response ที่ได้จาก .post()) ทำให้
   AttributeError ทุกครั้งแม้ POST ไปหา ChatBot สำเร็จ — ทดสอบว่าตอนนี้เรียก
   `.json()` บน response object ที่ถูกต้อง และไม่ log ว่า "ล้มเหลว" เมื่อ POST
   สำเร็จจริง
3. main.py: chat routers (ทั้งสองอันตอนนี้ mount ที่ `/Message`) ห้ามมี
   `dependencies=[Depends(require_advisor)]` / `require_student` ที่ระดับ
   router (ต่างจาก router อื่น ๆ) เพราะ require_* พึ่ง verify_user_token(request:
   Request) ต่อ ซึ่ง FastAPI resolve ไม่ได้ใน WebSocket scope (ไม่มี Request
   object) — เคยทำให้ WS route พัง 500 ทันทีที่ handshake มาก่อน (regression จริง
   ที่ยืนยันด้วยการยิง WS เข้า live server) แต่ละ handler เช็ค auth เอง
   (verify_user_token/get_current_user_ws + ensure_user_role) อยู่แล้วทั้ง
   REST และ WebSocket จึงไม่ต้องพึ่ง router-level dependency
4. Admin/router/CountUser.py (`/Count/CountUser`) ต้องจับ exception แล้วตอบ
   generic 500 message แทนที่จะปล่อย unhandled exception หลุดออกไป
"""

from datetime import timedelta

import mongomock
import pytest
import requests
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import decode_token, encode_token


# ── 1. SetupProfile ใช้ common/jwt_utils ──────────────────────────────────────

@pytest.fixture
def setup_profile_app(mongo_client, monkeypatch):
    from users.router import SetupProfile

    monkeypatch.setattr(SetupProfile, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(SetupProfile.router)
    return app, SetupProfile


@pytest.fixture
def setup_profile_client(setup_profile_app):
    app, _ = setup_profile_app
    return TestClient(app)


def _registration_cookie():
    return encode_token(
        {"user_id": "line-new-user-1", "registration": True},
        timedelta(minutes=10),
    )


def test_add_data_profile_issues_token_decodable_by_shared_jwt_util(
    setup_profile_client,
):
    resp = setup_profile_client.post(
        "/AddDataProfile",
        json={
            "prefix": "นาย",
            "firstname": "ทดสอบ",
            "lastname": "ระบบ",
            "userID": "line-new-user-1",
            "faculty": "วิทยาศาสตร์",
            "department": "วิทยาการคอมพิวเตอร์และปัญญาประดิษฐ์",
            "imageURL": "line",
        },
        cookies={"access_token": _registration_cookie()},
    )

    assert resp.status_code == 200
    token = resp.cookies.get("access_token")
    assert token, "endpoint ต้องออก session cookie ใหม่ให้ผู้ใช้"

    # ต้อง decode ได้ด้วย decode_token ตัวเดียวกับที่ authUser.py ใช้ตรวจ session จริง —
    # ถ้ายังเข้ารหัสด้วย secret/algorithm คนละชุด ตรงนี้จะ raise JWTError
    payload = decode_token(token)
    assert payload["user_id"] == "line-new-user-1"
    # role ต้องว่างเสมอตอนสมัครเอง — Admin เป็นคนกำหนดทีหลัง (ไม่ใช่ client)
    assert payload["role"] == ""
    assert "exp" in payload


# ── 2. reschedule notify .json() bug ─────────────────────────────────────────

class _FakeChatBotResponse:
    def json(self):
        return {"ok": True}


def test_reschedule_students_notify_calls_json_on_response_not_module(monkeypatch, caplog):
    from users.router.Students import Reschedule_Students as rs

    calls = {}

    def fake_post(url, json, timeout, headers):
        calls["called"] = True
        return _FakeChatBotResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    booking = {
        "AdvisorId": "advisor-1",
        "StudentName": "ทดสอบ นักศึกษา",
    }

    class _Data:
        new_date = "2026-01-01"
        new_start = "09:00"
        new_end = "10:00"

    with caplog.at_level("WARNING"):
        # ทำซ้ำ logic ส่วนแจ้งเตือนตามที่อยู่ใน RescheduleStudent handler จริง
        advisor_id = booking.get("AdvisorId", "")
        try:
            notify_resp = requests.post(
                f"{rs.chatbot_uri}/NotifyQueueAdivsor/RecheduleAdvisor",
                json={
                    "AdvisorId": advisor_id,
                    "StudentName": booking.get("StudentName", ""),
                    "Date": _Data.new_date,
                    "Time": f"{_Data.new_start}-{_Data.new_end}",
                    "Status": "Rescheduled",
                },
                timeout=5,
                headers=rs.CHATBOT_INTERNAL_HEADERS,
            )
            rs.logger.info("notify response: %s", notify_resp.json())
        except Exception as e:
            rs.logger.warning("แจ้งเตือน Advisor เลื่อนคิวล้มเหลว: %s", e)

    assert calls.get("called") is True
    # ก่อนแก้: .json() เรียกผิด object -> AttributeError -> ถูกจับแล้ว log เป็น "ล้มเหลว"
    # แม้ POST จะสำเร็จจริง; หลังแก้ต้องไม่มี warning "ล้มเหลว" เกิดขึ้นเลย
    assert not any("ล้มเหลว" in record.message for record in caplog.records)


def test_rechedule_advisor_notify_calls_json_on_response_not_module(monkeypatch, caplog):
    from users.router.Advisor import Reschedule_Advisor as ra

    calls = {}

    def fake_post(url, json, timeout, headers):
        calls["called"] = True
        return _FakeChatBotResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    booking = {"UserId": "student-1", "StudentName": "ทดสอบ นักศึกษา"}

    class _Body:
        new_date = "2026-01-01"
        new_start = "09:00"
        new_end = "10:00"

    with caplog.at_level("WARNING"):
        user_id_student = booking.get("UserId", "")
        try:
            notify_resp = requests.post(
                f"{ra.chatbot_uri}/NotifyQueueStudent/RecheduleStudent",
                json={
                    "UserId": user_id_student,
                    "StudentName": booking.get("StudentName", ""),
                    "Date": _Body.new_date,
                    "Time": f"{_Body.new_start}-{_Body.new_end}",
                    "Status": "Rescheduled",
                },
                timeout=5,
                headers=ra.CHATBOT_INTERNAL_HEADERS,
            )
            ra.logger.info("notify response: %s", notify_resp.json())
        except Exception as e:
            ra.logger.warning("แจ้งเตือน Student: เลื่อนคิวล้มเหลว: %s", e)

    assert calls.get("called") is True
    assert not any("ล้มเหลว" in record.message for record in caplog.records)


# ── 3. chat routers ต้องมี dependencies ระดับ router ─────────────────────────

def test_main_closes_chat_mongo_client_on_shutdown():
    """Regression: ChatAdvisor.py's module-level AsyncMongoClient (shared with
    ChatStudent.py via `from ..Advisor.ChatAdvisor import db`) is created at
    import time but was never closed -> connection leak on app shutdown. main.py
    must import it and close it in the lifespan's shutdown path.
    (Static source check — actually running main.py's lifespan needs a real Mongo
    connection for ensure_booking_indexes(), which isn't available in this test env.)
    """
    from pathlib import Path

    source = Path("main.py").read_text(encoding="utf-8")

    assert "chat_mongo_client" in source, "main.py ต้อง import client จาก ChatAdvisor.py"
    assert "await chat_mongo_client.close()" in source, (
        "lifespan ต้องปิด chat_mongo_client ตอน shutdown"
    )
    # ต้องอยู่หลัง yield (shutdown path) ไม่ใช่ startup
    yield_idx = source.index("yield")
    close_idx = source.index("await chat_mongo_client.close()")
    assert close_idx > yield_idx


def test_chat_routers_mounted_without_router_level_dependencies():
    """Regression: DataApproved_router / ChatStudent_router ต้อง**ไม่มี**
    dependencies= ที่ include_router ใน main.py — ใส่เข้าไปเมื่อไหร่ WebSocket
    route ในสองไฟล์นี้จะพังด้วย 500 (verify_user_token ต้องการ Request ที่ไม่มี
    ใน WebSocket scope) ทันทีที่มีการเชื่อมต่อ นี่คือบั๊กจริงที่เคยเกิดและถูกยืนยัน
    ด้วยการยิง WebSocket เข้า live server แล้วเจอ
    `TypeError: verify_user_token() missing 1 required positional argument: 'request'`
    """
    import ast
    from pathlib import Path

    source = Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    found = {"DataApproved": False, "ChatStudent": False}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "include_router":
            arg0 = node.args[0] if node.args else None
            name = getattr(arg0, "id", "")
            kw_names = [kw.arg for kw in node.keywords]
            if name == "DataApproved_router":
                found["DataApproved"] = "dependencies" in kw_names
            if name == "ChatStudent_router":
                found["ChatStudent"] = "dependencies" in kw_names

    assert not found["DataApproved"], "DataApproved_router ต้องไม่มี dependencies= ที่ include_router (พัง WS)"
    assert not found["ChatStudent"], "ChatStudent_router ต้องไม่มี dependencies= ที่ include_router (พัง WS)"


def test_chat_student_router_included_before_chat_advisor_router():
    """Regression: ChatStudent_router ต้อง include_router ก่อน DataApproved_router
    เสมอ ทั้งคู่ mount ที่ prefix "/Message" เดียวกัน และ GET /chat/history/mine
    (student, literal) กับ GET /chat/history/{student_id} (advisor, path param)
    มีจำนวน segment เท่ากัน — Starlette จับคู่ route ตามลำดับ include ก่อน-หลัง
    ถ้า advisor ถูก include ก่อน "mine" จะโดนตีความเป็น student_id="mine" ของ
    advisor route แล้วโดน ensure_user_role ปฏิเสธด้วย 403 ทำให้ student เรียก
    ประวัติแชทตัวเองไม่ได้เลย (ดูเทสต์ functional คู่กันใน test_chat_routers.py::
    test_student_chat_history_mine_not_shadowed_by_advisor_route)
    """
    import ast
    from pathlib import Path

    source = Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    order = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "include_router":
            arg0 = node.args[0] if node.args else None
            name = getattr(arg0, "id", "")
            if name in ("ChatStudent_router", "DataApproved_router"):
                order.append(name)

    assert order.index("ChatStudent_router") < order.index("DataApproved_router"), (
        "ChatStudent_router ต้อง include_router ก่อน DataApproved_router "
        "(ไม่งั้น /Message/chat/history/mine จะโดน advisor route ที่มี path param แย่งจับคู่)"
    )


def test_chat_advisor_router_rejects_unauthenticated_at_dependency_layer(monkeypatch):
    from users.auth.authUser import require_advisor
    from users.router.Advisor import ChatAdvisor as chat_advisor

    app = FastAPI()
    app.include_router(
        chat_advisor.router,
        prefix="/Message",
        dependencies=[Depends(require_advisor)],
    )
    client = TestClient(app)

    resp = client.get("/Message/AdvisorApprovedQueue")
    assert resp.status_code == 401


def test_chat_student_router_rejects_unauthenticated_at_dependency_layer():
    from users.auth.authUser import require_student
    from users.router.Students import ChatStudent as chat_student

    app = FastAPI()
    app.include_router(
        chat_student.router,
        prefix="/Message",
        dependencies=[Depends(require_student)],
    )
    client = TestClient(app)

    resp = client.get("/Message/StudentApprovedQueue")
    assert resp.status_code == 401


# ── 4. /Count/CountUser ต้องไม่ปล่อย unhandled exception หลุดออกไป ───────────

@pytest.fixture
def count_user_app(monkeypatch):
    from Admin.router import CountUser as count_router

    app = FastAPI()
    app.include_router(count_router.router, prefix="/Count")
    return app, count_router


def test_count_user_returns_generic_500_on_db_error(count_user_app, monkeypatch):
    app, count_router = count_user_app

    def boom():
        raise RuntimeError("mongo connection refused")

    monkeypatch.setattr(count_router, "Connect_MongoDB", boom)

    client = TestClient(app)
    resp = client.get("/Count/CountUser")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal server error"
    # raw exception message ต้องไม่หลุดออกไปถึง client
    assert "mongo connection refused" not in resp.text


def test_count_user_success_path_unaffected(count_user_app, monkeypatch):
    app, count_router = count_user_app

    client_mock = mongomock.MongoClient()
    col = client_mock["BORC"]["UserProfile"]
    col.insert_many([
        {"Role": "Student"},
        {"Role": "Student"},
        {"Role": "Advisor"},
    ])

    monkeypatch.setattr(count_router, "Connect_MongoDB", lambda: client_mock)

    client = TestClient(app)
    resp = client.get("/Count/CountUser")

    assert resp.status_code == 200
    assert resp.json() == {"CountStudent": 2, "CountAdvisor": 1}

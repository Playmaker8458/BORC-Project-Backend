"""
Endpoint coverage สำหรับ chat routers ที่ก่อนหน้านี้ไม่มี test เลย (นอกเหนือจาก
regression tests แคบ ๆ ใน test_security_fixes.py):

- users/router/Students/ChatStudent.py  (StudentApprovedQueue, chat/history/mine, WS auth boundary)
- users/router/Advisor/testChatAdvisor.py (AdvisorApprovedQueue, chat/history/{id}, chat/message, WS auth boundary)

ทั้งสองไฟล์ใช้ `db = AsyncMongoClient(...)` (pymongo async driver, module-level
global ที่ต่อ Mongo จริงตอน import) แทน `Connect_MongoDB()` sync ปกติ ดังนั้น
เราสร้าง thin async wrapper รอบ mongomock (sync) แล้ว monkeypatch แอตทริบิวต์
`db` ของแต่ละ module โดยตรง (ทั้งสองไฟล์ import `db` เป็นชื่อของตัวเองตอน
import time เลยต้อง patch แยกทีละ module).
"""

from datetime import timedelta

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from common.jwt_utils import encode_token


def _make_token(user_id: str) -> str:
    return encode_token({"user_id": user_id}, timedelta(days=1))


def _seed_user(mongo_client, user_id, role, status="Approved"):
    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": user_id,
        "Role": role,
        "Status": status,
        "Prefix": "",
        "Firstname": "ทดสอบ",
        "Lastname": "ระบบ",
        "imageURL": "",
    })


def _cookies(user_id: str) -> dict:
    return {"access_token": _make_token(user_id)}


# ── async wrapper around mongomock (sync) so `await db[...].find_one(...)` works ──

class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def sort(self, *a, **kw):
        self._cursor = self._cursor.sort(*a, **kw)
        return self

    async def to_list(self, length=None):
        return list(self._cursor)


class _AsyncCollection:
    def __init__(self, col):
        self._col = col

    async def find_one(self, *a, **kw):
        return self._col.find_one(*a, **kw)

    def find(self, *a, **kw):
        return _AsyncCursor(self._col.find(*a, **kw))

    async def insert_one(self, doc):
        return self._col.insert_one(doc)


class _AsyncDB:
    def __init__(self, sync_db):
        self._db = sync_db

    def __getitem__(self, name):
        return _AsyncCollection(self._db[name])


def _insert_booking(mongo_client, **kw):
    doc = {
        "UserId": "student-1",
        "AdvisorId": "advisor-1",
        "StudentName": "นาย ทดสอบ นักศึกษา",
        "Advisor_Name": "อ.ทดสอบ",
        "Date": "2099-01-01",
        "Time": "09:00-10:00",
        "Status": "Approved",
    }
    doc.update(kw)
    return mongo_client["BORC"]["BookingOnline"].insert_one(doc)


# ── ChatStudent.py ──────────────────────────────────────────────────────────────

@pytest.fixture
def chat_student_app(mongo_client, monkeypatch):
    from users.router.Students import ChatStudent as cs
    from users.router.Advisor import testChatAdvisor as ca
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    async_db = _AsyncDB(mongo_client["BORC"])
    # both modules bound `db` as their own module-level name at import time
    monkeypatch.setattr(cs, "db", async_db)
    monkeypatch.setattr(ca, "db", async_db)

    # ⚠️ ห้ามใส่ dependencies=[Depends(require_student)] ตรงนี้ — require_student
    # ไปเรียก verify_user_token(request: Request) ต่อ ซึ่ง FastAPI resolve ไม่ได้ใน
    # WebSocket scope (ไม่มี Request object) ทำให้ WS route พังด้วย 500 ทันที
    # (บั๊กจริงที่เคยเกิดกับ main.py — ดู test_chat_routers_mounted_without_router_level_dependencies
    # ใน test_security_fixes.py) แต่ละ handler เช็ค auth เองอยู่แล้วทั้ง REST และ WS
    app = FastAPI()
    app.include_router(cs.router)
    return app, cs


@pytest.fixture
def chat_student_client(chat_student_app):
    app, _ = chat_student_app
    return TestClient(app)


def test_student_approved_queue_returns_own_bookings(chat_student_client, mongo_client):
    _seed_user(mongo_client, "student-1", "Student")
    _insert_booking(mongo_client, UserId="student-1", Status="Approved")
    _insert_booking(mongo_client, UserId="student-2", Status="Approved")

    resp = chat_student_client.get("/StudentApprovedQueue", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    queues = resp.json()["queues"]
    assert len(queues) == 1
    assert queues[0]["UserId"] == "student-1"


def test_student_approved_queue_requires_auth(chat_student_client):
    resp = chat_student_client.get("/StudentApprovedQueue")
    assert resp.status_code == 401


def test_chat_history_mine_empty_when_no_active_booking(chat_student_client, mongo_client):
    _seed_user(mongo_client, "student-1", "Student")

    resp = chat_student_client.get("/chat/history/mine", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == []


def test_chat_history_mine_returns_messages_with_own_advisor(chat_student_client, mongo_client):
    _seed_user(mongo_client, "student-1", "Student")
    _insert_booking(mongo_client, UserId="student-1", AdvisorId="advisor-1", Status="Approved")
    mongo_client["BORC"]["ChatMessages"].insert_one({
        "student_id": "student-1",
        "advisor_id": "advisor-1",
        "sender": "teacher",
        "type": "text",
        "text": "hello",
        "timestamp": None,
    })

    resp = chat_student_client.get("/chat/history/mine", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["text"] == "hello"


def test_chat_history_mine_logs_warning_when_booking_missing_advisor_id(
    chat_student_client, mongo_client, caplog
):
    """Regression: resolve_advisor_id_for_student ต้อง log warning เมื่อ booking
    active มีอยู่จริงแต่ไม่มี AdvisorId (data integrity issue) แทนที่จะเงียบเหมือน
    กรณี "ไม่มี booking เลย" — response ยังคงเป็น [] เหมือนเดิม (ไม่เปลี่ยน behavior)"""
    _seed_user(mongo_client, "student-1", "Student")
    _insert_booking(mongo_client, UserId="student-1", AdvisorId="", Status="Approved")

    with caplog.at_level("WARNING"):
        resp = chat_student_client.get("/chat/history/mine", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == []
    assert any(
        "no AdvisorId" in record.message and "student-1" in record.message
        for record in caplog.records
    )


def test_chat_history_mine_no_warning_when_simply_no_booking(
    chat_student_client, mongo_client, caplog
):
    """Contrast case: ไม่มี booking active เลย -> ไม่ใช่ข้อมูลผิดปกติ ไม่ควร log warning"""
    _seed_user(mongo_client, "student-1", "Student")

    with caplog.at_level("WARNING"):
        resp = chat_student_client.get("/chat/history/mine", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == []
    assert not any("no AdvisorId" in record.message for record in caplog.records)


def test_student_ws_rejected_when_no_active_booking(chat_student_client, mongo_client):
    """Regression: ต้องถูกปฏิเสธด้วย WS_1008_POLICY_VIOLATION (ไม่มี booking active)
    ไม่ใช่ 500 TypeError จาก router-level dependencies ที่พังใน WebSocket scope
    (ดู test_security_fixes.py::test_chat_routers_mounted_without_router_level_dependencies)"""
    _seed_user(mongo_client, "student-1", "Student")
    token = _make_token("student-1")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with chat_student_client.websocket_connect(
            "/chat/ws/student/student-1", cookies={"access_token": token}
        ):
            pass
    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION


# ── testChatAdvisor.py ────────────────────────────────────────────────────────

@pytest.fixture
def chat_advisor_app(mongo_client, monkeypatch):
    from users.router.Advisor import testChatAdvisor as ca
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ca, "db", _AsyncDB(mongo_client["BORC"]))

    # ⚠️ ห้ามใส่ dependencies=[Depends(require_advisor)] ตรงนี้ — เหตุผลเดียวกับ
    # chat_student_app fixture ด้านบน (require_advisor -> verify_user_token(Request)
    # พังใน WebSocket scope)
    app = FastAPI()
    app.include_router(ca.router)
    return app, ca


@pytest.fixture
def chat_advisor_client(chat_advisor_app):
    app, _ = chat_advisor_app
    return TestClient(app)


def test_data_approved_returns_own_bookings_only(chat_advisor_client, mongo_client):
    _seed_user(mongo_client, "advisor-1", "Advisor")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved")
    _insert_booking(mongo_client, AdvisorId="advisor-2", Status="Approved")

    resp = chat_advisor_client.get("/AdvisorApprovedQueue", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    queues = resp.json()["queues"]
    assert len(queues) == 1
    assert queues[0]["AdvisorId"] == "advisor-1"


def test_data_approved_requires_auth(chat_advisor_client):
    resp = chat_advisor_client.get("/AdvisorApprovedQueue")
    assert resp.status_code == 401


def test_send_chat_message_persists_and_broadcasts(chat_advisor_client, mongo_client):
    _seed_user(mongo_client, "advisor-1", "Advisor")
    # ต้องมีคิวร่วมกัน (Step 3: อาจารย์ส่งข้อความถึงนักศึกษาที่ไม่มีคิวกับตนไม่ได้)
    _insert_booking(mongo_client, UserId="student-1", AdvisorId="advisor-1", Status="Approved")

    resp = chat_advisor_client.post(
        "/chat/message",
        json={"student_id": "student-1", "text": "hi there"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    msg = mongo_client["BORC"]["ChatMessages"].find_one({"student_id": "student-1"})
    assert msg["text"] == "hi there"
    assert msg["sender"] == "teacher"


def test_advisor_ws_rejected_when_no_matching_booking(chat_advisor_client, mongo_client):
    """Regression: เหตุผลเดียวกับ test_student_ws_rejected_when_no_active_booking"""
    _seed_user(mongo_client, "advisor-1", "Advisor")
    token = _make_token("advisor-1")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with chat_advisor_client.websocket_connect(
            "/chat/ws/advisor/no-such-student", cookies={"access_token": token}
        ):
            pass
    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION


# ── Regression: route-order collision ระหว่าง student/advisor router ─────────
# GET /chat/history/mine (student, literal path) กับ GET /chat/history/{student_id}
# (advisor, path param) มีจำนวน segment เท่ากัน เมื่อ mount ทั้งคู่ไว้ใต้ prefix
# "/Message" เดียวกัน Starlette จะจับคู่ route ตามลำดับ include_router ก่อน-หลัง
# ถ้า advisor router ถูก include ก่อน "mine" จะโดนตีความเป็น student_id="mine"
# ของ advisor route ไปเลย แล้วโดน ensure_user_role(payload, "Advisor") ปฏิเสธด้วย
# 403 — student จะเรียกประวัติแชทของตัวเองไม่ได้เลย (เจอจริงจากการทดสอบผ่านหน้าเว็บ)
# ดังนั้นต้อง include ChatStudent_router (literal path) ก่อน DataApproved_router
# (path-param) เสมอ — เทสต์นี้ประกอบ app แบบเดียวกับ main.py จริงเพื่อล็อกพฤติกรรมนี้ไว้

@pytest.fixture
def combined_chat_app(mongo_client, monkeypatch):
    from users.router.Students import ChatStudent as cs
    from users.router.Advisor import testChatAdvisor as ca
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    async_db = _AsyncDB(mongo_client["BORC"])
    monkeypatch.setattr(cs, "db", async_db)
    monkeypatch.setattr(ca, "db", async_db)

    app = FastAPI()
    # ลำดับนี้ต้องตรงกับ main.py เป๊ะ ๆ (student ก่อน advisor) — ห้ามสลับ
    app.include_router(cs.router, prefix="/Message")
    app.include_router(ca.router, prefix="/Message")
    return TestClient(app)


def test_student_chat_history_mine_not_shadowed_by_advisor_route(combined_chat_app, mongo_client):
    _seed_user(mongo_client, "student-1", "Student")
    _insert_booking(mongo_client, UserId="student-1", AdvisorId="advisor-1", Status="Approved")

    resp = combined_chat_app.get("/Message/chat/history/mine", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == []


def test_advisor_chat_history_by_id_still_reachable_after_student_router(combined_chat_app, mongo_client):
    _seed_user(mongo_client, "advisor-1", "Advisor")
    _insert_booking(mongo_client, UserId="student-1", AdvisorId="advisor-1", Status="Approved")

    resp = combined_chat_app.get("/Message/chat/history/student-1", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    assert resp.json() == []

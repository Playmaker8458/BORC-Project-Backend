"""
Step 5 ของการเสริมความปลอดภัย: จำกัดขนาด/ความถี่ของแชท และตรวจ imageURL ตอนสมัคร

ช่องโหว่ที่พิสูจน์ได้จากการเจาะระบบในเครื่อง:
- ข้อความแชท 2-3 MB ผ่านและถูกเก็บลง DB (ทั้ง WebSocket และ REST) — ผู้ใช้ที่ล็อกอินอัดข้อมูลเต็ม DB ได้
- ส่งข้อความรัว 60 ครั้งติดโดยไม่ถูกจำกัด
- imageURL ตอนสมัครยาว 500 KB / เป็น javascript: ก็ถูกเก็บ

ค่าเริ่มต้น (ปรับได้ด้วย env): ข้อความ ≤ 2000 ตัวอักษร (CHAT_MAX_TEXT_LENGTH),
15 ข้อความ / 10 วินาที ต่อผู้ใช้ (CHAT_RATE_MAX, CHAT_RATE_WINDOW_SEC), imageURL ≤ 500 ตัวอักษรและเป็น https
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from common import chat_limits
from common.chat_limits import ChatInvalid, SlidingWindowLimiter, parse_ws_text
from tests.test_chat_routers import _AsyncDB, _cookies, _insert_booking, _seed_user

LIMIT = 2000


def _frame(text):
    return json.dumps({"text": text})


# ── parse_ws_text ────────────────────────────────────────────────────────────

def test_parse_returns_text_for_valid_frame():
    assert parse_ws_text(_frame("สวัสดีครับ")) == "สวัสดีครับ"


def test_parse_accepts_exactly_the_limit_and_rejects_one_over():
    assert parse_ws_text(_frame("a" * LIMIT)) == "a" * LIMIT
    with pytest.raises(ChatInvalid) as exc:
        parse_ws_text(_frame("a" * (LIMIT + 1)))
    assert exc.value.code == 1009


def test_parse_counts_characters_not_bytes():
    thai = "ก" * LIMIT  # 3 ไบต์ต่อตัวอักษรใน UTF-8 แต่ยังไม่เกินขีดจำกัดตัวอักษร
    assert parse_ws_text(_frame(thai)) == thai


def test_parse_rejects_giant_raw_frame_without_parsing_it():
    with pytest.raises(ChatInvalid) as exc:
        parse_ws_text(_frame("A" * 3_000_000))
    assert exc.value.code == 1009


@pytest.mark.parametrize("raw", ["not json", "[1,2]", "123", "null", '{"msg": "x"}', '{"text": 5}', '{"text": null}', ""])
def test_parse_rejects_malformed_payloads(raw):
    with pytest.raises(ChatInvalid) as exc:
        parse_ws_text(raw)
    assert exc.value.code == 1007


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_parse_returns_none_for_blank_text(text):
    assert parse_ws_text(_frame(text)) is None


def test_limit_is_configurable_by_env(monkeypatch):
    monkeypatch.setenv("CHAT_MAX_TEXT_LENGTH", "10")
    assert parse_ws_text(_frame("a" * 10)) == "a" * 10
    with pytest.raises(ChatInvalid):
        parse_ws_text(_frame("a" * 11))


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "", "999999999"])
def test_bad_env_values_fall_back_to_safe_defaults(monkeypatch, bad):
    monkeypatch.setenv("CHAT_MAX_TEXT_LENGTH", bad)
    assert 1 <= chat_limits.max_text_length() <= 20000


# ── SlidingWindowLimiter ─────────────────────────────────────────────────────

def test_limiter_allows_up_to_max_then_blocks_then_recovers():
    now = [1000.0]
    lim = SlidingWindowLimiter(clock=lambda: now[0])
    assert all(lim.allow("u1", max_events=3, window_sec=10) for _ in range(3))
    assert lim.allow("u1", max_events=3, window_sec=10) is False
    now[0] += 10.1
    assert lim.allow("u1", max_events=3, window_sec=10) is True


def test_limiter_keys_are_independent():
    lim = SlidingWindowLimiter(clock=lambda: 1.0)
    assert lim.allow("a", max_events=1, window_sec=10)
    assert lim.allow("a", max_events=1, window_sec=10) is False
    assert lim.allow("b", max_events=1, window_sec=10)


def test_limiter_does_not_grow_unbounded_for_idle_keys():
    now = [0.0]
    lim = SlidingWindowLimiter(clock=lambda: now[0])
    for i in range(500):
        lim.allow("k%d" % i, max_events=1, window_sec=1)
    now[0] = 100.0
    lim.allow("fresh", max_events=1, window_sec=1)
    assert len(lim._events) < 50


# ── WebSocket (นักศึกษา / อาจารย์) ───────────────────────────────────────────

@pytest.fixture
def chat(mongo_client, monkeypatch):
    from users.router.Advisor import ChatAdvisor as ca
    from users.router.Students import ChatStudent as cs
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    adb = _AsyncDB(mongo_client["BORC"])
    monkeypatch.setattr(ca, "db", adb)
    monkeypatch.setattr(cs, "db", adb)
    chat_limits.chat_limiter.reset()

    app = FastAPI()
    app.include_router(cs.router, prefix="/Message")
    app.include_router(ca.router, prefix="/Message")
    _seed_user(mongo_client, "student-1", "Student")
    _seed_user(mongo_client, "advisor-1", "Advisor")
    _insert_booking(mongo_client, UserId="student-1", AdvisorId="advisor-1", Status="Approved")
    return TestClient(app), mongo_client


def _stored(mongo):
    return [m["text"] for m in mongo["BORC"]["ChatMessages"].find({})]


STUDENT_WS = "/Message/chat/ws/student/student-1"
ADVISOR_WS = "/Message/chat/ws/advisor/student-1"


@pytest.mark.parametrize("path,who", [(STUDENT_WS, "student-1"), (ADVISOR_WS, "advisor-1")])
def test_ws_normal_message_is_stored_and_echoed(chat, path, who):
    client, mongo = chat
    with client.websocket_connect(path, cookies=_cookies(who)) as ws:
        ws.send_text(_frame("สวัสดี"))
        assert ws.receive_json()["text"] == "สวัสดี"
    assert _stored(mongo) == ["สวัสดี"]


@pytest.mark.parametrize("path,who", [(STUDENT_WS, "student-1"), (ADVISOR_WS, "advisor-1")])
def test_ws_oversized_message_closes_connection_and_is_not_stored(chat, path, who):
    client, mongo = chat
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, cookies=_cookies(who)) as ws:
            ws.send_text(_frame("A" * 2_000_000))
            ws.receive_text()
    assert exc.value.code == 1009
    assert _stored(mongo) == []


@pytest.mark.parametrize("path,who", [(STUDENT_WS, "student-1"), (ADVISOR_WS, "advisor-1")])
def test_ws_malformed_frame_closes_with_1007(chat, path, who):
    client, mongo = chat
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, cookies=_cookies(who)) as ws:
            ws.send_text("this is not json")
            ws.receive_text()
    assert exc.value.code == 1007
    assert _stored(mongo) == []


@pytest.mark.parametrize("path,who", [(STUDENT_WS, "student-1"), (ADVISOR_WS, "advisor-1")])
def test_ws_blank_message_is_ignored_and_connection_stays_open(chat, path, who):
    client, mongo = chat
    with client.websocket_connect(path, cookies=_cookies(who)) as ws:
        ws.send_text(_frame("   "))
        ws.send_text(_frame("จริง"))
        assert ws.receive_json()["text"] == "จริง"
    assert _stored(mongo) == ["จริง"]


@pytest.mark.parametrize("path,who", [(STUDENT_WS, "student-1"), (ADVISOR_WS, "advisor-1")])
def test_ws_flood_is_cut_off_after_the_rate_limit(chat, monkeypatch, path, who):
    monkeypatch.setenv("CHAT_RATE_MAX", "3")
    client, mongo = chat
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, cookies=_cookies(who)) as ws:
            for i in range(10):
                ws.send_text(_frame("msg%d" % i))
            for _ in range(10):
                ws.receive_text()
    assert exc.value.code == 1008
    assert _stored(mongo) == ["msg0", "msg1", "msg2"]


def test_ws_rate_limit_is_per_user_not_global(chat, monkeypatch):
    monkeypatch.setenv("CHAT_RATE_MAX", "2")
    client, mongo = chat
    _seed_user(mongo, "student-2", "Student")
    _insert_booking(mongo, UserId="student-2", AdvisorId="advisor-1", Status="Approved")
    with client.websocket_connect(STUDENT_WS, cookies=_cookies("student-1")) as ws:
        ws.send_text(_frame("a1"))
        ws.send_text(_frame("a2"))
        ws.receive_text(); ws.receive_text()
    with client.websocket_connect("/Message/chat/ws/student/student-2", cookies=_cookies("student-2")) as ws:
        ws.send_text(_frame("b1"))
        assert ws.receive_json()["text"] == "b1"


# ── REST ของอาจารย์ ──────────────────────────────────────────────────────────

def _rest_text(client, text, who="advisor-1"):
    return client.post("/Message/chat/message", json={"student_id": "student-1", "text": text}, cookies=_cookies(who))


def test_rest_message_over_limit_is_rejected(chat):
    client, mongo = chat
    assert _rest_text(client, "B" * 3_000_000).status_code == 400
    assert _rest_text(client, "B" * (LIMIT + 1)).status_code == 400
    assert _stored(mongo) == []


def test_rest_message_at_limit_is_accepted(chat):
    client, mongo = chat
    assert _rest_text(client, "B" * LIMIT).status_code == 200


def test_rest_blank_message_is_rejected(chat):
    client, mongo = chat
    assert _rest_text(client, "   ").status_code == 400
    assert _stored(mongo) == []


def test_rest_flood_gets_429_with_retry_after(chat, monkeypatch):
    monkeypatch.setenv("CHAT_RATE_MAX", "3")
    client, mongo = chat
    codes = [_rest_text(client, "spam%d" % i).status_code for i in range(6)]
    assert codes == [200, 200, 200, 429, 429, 429]
    blocked = _rest_text(client, "x")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert len(_stored(mongo)) == 3


def test_rest_appointment_flood_gets_429_and_stops_chatbot_pushes(chat, monkeypatch):
    import httpx
    pushes = []

    async def fake_post(self, url, **kw):
        pushes.append(kw.get("json"))

        class _R:
            status_code = 200
        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("CHAT_RATE_MAX", "2")
    client, mongo = chat
    codes = [client.post("/Message/chat/appointment", json={"student_id": "student-1", "url": "https://zoom.us/j/%d" % i},
                         cookies=_cookies("advisor-1")).status_code for i in range(5)]
    assert codes == [200, 200, 429, 429, 429]
    assert len(pushes) == 2


def test_rate_limit_shared_between_ws_and_rest_for_same_user(chat, monkeypatch):
    monkeypatch.setenv("CHAT_RATE_MAX", "2")
    client, mongo = chat
    assert _rest_text(client, "r1").status_code == 200
    assert _rest_text(client, "r2").status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(ADVISOR_WS, cookies=_cookies("advisor-1")) as ws:
            ws.send_text(_frame("ws1"))
            ws.receive_text()


# ── imageURL ตอนสมัคร ────────────────────────────────────────────────────────

@pytest.fixture
def setup_client(mongo_client, monkeypatch):
    from users.router import SetupProfile
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(SetupProfile, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.include_router(SetupProfile.router, prefix="/SetupProfile")
    return TestClient(app), mongo_client


def _register(client, **over):
    from datetime import timedelta
    from common.jwt_utils import encode_token

    body = {"prefix": "นาย", "firstname": "เอ", "lastname": "บี", "userID": "newbie",
            "faculty": "วิทยาศาสตร์", "department": "วิทยาการคอมพิวเตอร์และปัญญาประดิษฐ์"}
    body.update(over)
    token = encode_token({"user_id": "newbie", "registration": True}, timedelta(minutes=15))
    return client.post("/SetupProfile/AddDataProfile", json=body, cookies={"access_token": token})


@pytest.mark.parametrize("value", ["https://profile.line-scdn.net/0h_abc/pic.jpg", "line", ""])
def test_image_url_accepts_line_picture_default_and_empty(setup_client, value):
    client, mongo = setup_client
    resp = _register(client, imageURL=value)
    assert resp.status_code == 200
    assert mongo["BORC"]["UserProfile"].find_one({"userId": "newbie"})["imageURL"] == value


def test_image_url_omitted_defaults_to_line(setup_client):
    client, mongo = setup_client
    assert _register(client).status_code == 200
    assert mongo["BORC"]["UserProfile"].find_one({"userId": "newbie"})["imageURL"] == "line"


def test_image_url_null_defaults_to_line(setup_client):
    client, mongo = setup_client
    assert _register(client, imageURL=None).status_code == 200
    assert mongo["BORC"]["UserProfile"].find_one({"userId": "newbie"})["imageURL"] == "line"


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "data:image/png;base64,AAAA", "http://profile.line-scdn.net/pic.jpg",
    "https://user:pw@evil.example/x.png", "https://evil.example/" + "a" * 500, "https://evil.example/ x.png", "not a url",
])
def test_image_url_rejects_unsafe_or_oversized_values(setup_client, value):
    client, mongo = setup_client
    resp = _register(client, imageURL=value)
    assert resp.status_code == 422
    assert mongo["BORC"]["UserProfile"].count_documents({}) == 0


def test_image_url_at_500_chars_is_accepted(setup_client):
    client, _ = setup_client
    url = "https://profile.line-scdn.net/" + "a" * (500 - len("https://profile.line-scdn.net/"))
    assert len(url) == 500
    assert _register(client, imageURL=url).status_code == 200

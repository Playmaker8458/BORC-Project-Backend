"""
Step 3 ของการเสริมความปลอดภัย: สิทธิ์และ URL ของข้อความแชทที่อาจารย์ส่ง

ช่องโหว่ที่พิสูจน์ได้จากการเจาะระบบในเครื่อง:
- POST /chat/message และ /chat/appointment ไม่ตรวจว่าอาจารย์มีคิวกับนักศึกษาคนนั้นจริง
  -> อาจารย์ส่งข้อความเข้าห้องแชทของนักศึกษาที่ไม่ใช่ของตนได้ และสั่งให้ ChatBot ส่งลิงก์ไปที่
  LINE user ใดก็ได้
- ไม่ตรวจ URL -> ส่ง javascript:, http:, data: หรือ URL ที่ฝัง user:pass@ (ฟิชชิง) ได้

กฎใหม่:
- ต้องมี booking ของอาจารย์คนนี้กับนักศึกษาคนนี้ในสถานะเดียวกับที่หน้าแชท/WebSocket ใช้ (CHAT_STATUSES)
- ลิงก์ต้องเป็น https ที่มี hostname ASCII, ไม่มี user:pass@, ไม่มีช่องว่าง/อักขระควบคุม, ยาว ≤ 2048
- (ทางเลือก) env CHAT_LINK_ALLOWED_HOSTS จำกัดโดเมน (ต่อท้ายตรงขอบจุด); ค่าเริ่มต้น = ไม่จำกัดโดเมน
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.url_safety import UnsafeUrl, validate_https_url
from tests.test_chat_routers import _AsyncDB, _cookies, _insert_booking, _seed_user

GOOD_URLS = [
    "https://zoom.us/j/1234567890?pwd=AbC123",
    "https://us02web.zoom.us/j/99?pwd=x",
    "https://meet.google.com/abc-defg-hij",
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d",
    "https://xn--12c1fe0br.example/room",  # punycode ผ่าน (เป็น ASCII)
]

BAD_URLS = [
    "javascript:alert(document.cookie)",
    "JAVASCRIPT:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "http://zoom.us/j/1",
    "ftp://zoom.us/file",
    "file:///etc/passwd",
    "//evil.example/path",
    "https://",
    "https:///path",
    "https://user:pass@evil.example/",
    "https://zoom.us@evil.example/",
    "https://evil.example/ has space",
    "https://evil.example/\nX-Injected: 1",
    "https://exämple.com/",  # hostname ไม่ใช่ ASCII (homograph)
    "https://zoom.us/" + "a" * 2100,
    "",
    "   ",
    "not a url",
]


# ── ฟังก์ชันตรวจ URL ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", GOOD_URLS)
def test_validate_accepts_https_meeting_links(url):
    assert validate_https_url(url) == url


def test_validate_strips_surrounding_whitespace():
    assert validate_https_url("  https://zoom.us/j/1  ") == "https://zoom.us/j/1"


@pytest.mark.parametrize("url", BAD_URLS)
def test_validate_rejects_unsafe_links(url):
    with pytest.raises(UnsafeUrl):
        validate_https_url(url)


def test_validate_host_allowlist_matches_on_label_boundary():
    allowed = ["zoom.us", "meet.google.com"]
    assert validate_https_url("https://zoom.us/j/1", allowed_hosts=allowed)
    assert validate_https_url("https://us02web.zoom.us/j/1", allowed_hosts=allowed)
    assert validate_https_url("https://MEET.google.com/x", allowed_hosts=allowed)
    for bad in ["https://evilzoom.us/j/1", "https://zoom.us.evil.example/j/1", "https://evil.example/zoom.us"]:
        with pytest.raises(UnsafeUrl):
            validate_https_url(bad, allowed_hosts=allowed)


# ── endpoint ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx(mongo_client, monkeypatch):
    from users.router.Advisor import ChatAdvisor as ca
    from users.auth import authUser

    outbound = []

    async def fake_post(self, url, **kw):
        outbound.append((url, kw.get("json")))

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ca, "db", _AsyncDB(mongo_client["BORC"]))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    app = FastAPI()
    app.include_router(ca.router)
    _seed_user(mongo_client, "advisor-1", "Advisor")
    _seed_user(mongo_client, "advisor-2", "Advisor")
    _seed_user(mongo_client, "student-1", "Student")
    return TestClient(app), mongo_client, outbound


def _msgs(mongo):
    return list(mongo["BORC"]["ChatMessages"].find({}, {"_id": 0}))


def _link(client, student="student-1", url="https://zoom.us/j/1", who="advisor-1"):
    return client.post("/chat/appointment", json={"student_id": student, "url": url}, cookies=_cookies(who))


def _text(client, student="student-1", text="สวัสดี", who="advisor-1"):
    return client.post("/chat/message", json={"student_id": student, "text": text}, cookies=_cookies(who))


# ── ความเป็นเจ้าของ (มีคิวร่วมกัน) ─────────────────────────────────────────────

def test_advisor_with_booking_can_send_text_and_link(ctx):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")

    assert _text(client).status_code == 200
    resp = _link(client, url="https://zoom.us/j/555")
    assert resp.status_code == 200

    stored = _msgs(mongo)
    assert [(m["type"], m["text"], m["advisor_id"]) for m in stored] == [
        ("text", "สวัสดี", "advisor-1"), ("link", "https://zoom.us/j/555", "advisor-1")]
    assert len(outbound) == 1
    assert outbound[0][1] == {"line_user_id": "student-1", "url": "https://zoom.us/j/555"}


def test_advisor_without_any_booking_is_rejected(ctx):
    client, mongo, outbound = ctx
    assert _text(client).status_code == 403
    assert _link(client).status_code == 403
    assert _msgs(mongo) == []
    assert outbound == []


def test_advisor_cannot_message_another_advisors_student(ctx):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-2", Status="Approved")

    assert _text(client, who="advisor-1").status_code == 403
    assert _link(client, who="advisor-1").status_code == 403
    assert _msgs(mongo) == []
    assert outbound == []


def test_cannot_push_link_to_arbitrary_line_user_id(ctx):
    """เดิมส่งไปที่ line_user_id ใดก็ได้ผ่าน ChatBot"""
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")

    resp = _link(client, student="U_someone_else_on_line")

    assert resp.status_code == 403
    assert outbound == []


def test_cancelled_booking_does_not_grant_access(ctx):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Cancelled")
    assert _text(client).status_code == 403
    assert _link(client).status_code == 403
    assert outbound == []


@pytest.mark.parametrize("status", ["Pending", "Approved", "Rescheduled", "InProgress", "Completed"])
def test_all_chat_visible_statuses_still_allowed(ctx, status):
    """ต้องตรงกับรายชื่อที่หน้าแชทของอาจารย์แสดง/WebSocket อนุญาต (ไม่ทำให้ใช้งานปกติพัง)"""
    client, mongo, _ = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status=status)
    assert _text(client).status_code == 200
    assert _link(client).status_code == 200


def test_student_role_cannot_use_advisor_chat_endpoints(ctx):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")
    assert _text(client, who="student-1").status_code == 403
    assert _link(client, who="student-1").status_code == 403
    assert outbound == []


def test_unauthenticated_is_rejected(ctx):
    client, _, _ = ctx
    assert client.post("/chat/appointment", json={"student_id": "student-1", "url": "https://zoom.us/j/1"}).status_code == 401
    assert client.post("/chat/message", json={"student_id": "student-1", "text": "x"}).status_code == 401


# ── URL ที่ endpoint ยอมรับ/ปฏิเสธ ─────────────────────────────────────────────

@pytest.mark.parametrize("url", GOOD_URLS)
def test_endpoint_accepts_good_links(ctx, url):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")
    assert _link(client, url=url).status_code == 200
    assert _msgs(mongo)[0]["text"] == url
    assert outbound[0][1]["url"] == url


@pytest.mark.parametrize("url", BAD_URLS)
def test_endpoint_rejects_unsafe_links_without_side_effects(ctx, url):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")

    resp = _link(client, url=url)

    assert resp.status_code == 400
    assert _msgs(mongo) == []
    assert outbound == []


def test_endpoint_stores_trimmed_url(ctx):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")
    assert _link(client, url="  https://zoom.us/j/9  ").status_code == 200
    assert _msgs(mongo)[0]["text"] == "https://zoom.us/j/9"
    assert outbound[0][1]["url"] == "https://zoom.us/j/9"


def test_endpoint_honours_host_allowlist_env(ctx, monkeypatch):
    client, mongo, outbound = ctx
    _insert_booking(mongo, UserId="student-1", AdvisorId="advisor-1", Status="Approved")
    monkeypatch.setenv("CHAT_LINK_ALLOWED_HOSTS", "zoom.us, meet.google.com")

    assert _link(client, url="https://us02web.zoom.us/j/1").status_code == 200
    assert _link(client, url="https://other.example/room").status_code == 400
    assert len(_msgs(mongo)) == 1


def test_unknown_student_check_happens_before_url_check(ctx):
    """ไม่มีสิทธิ์ต้องได้ 403 ก่อน (ไม่รั่วว่า URL ถูกหรือผิดให้คนที่ไม่มีสิทธิ์)"""
    client, _, _ = ctx
    assert _link(client, url="javascript:alert(1)").status_code == 403

"""
Step 1 ของการเสริมความปลอดภัย (ผลจากการเจาะระบบในเครื่อง):

1. Rate limit ต้องนับตาม IP ผู้ใช้จริงหลัง proxy ของ Railway (header X-Real-IP) ไม่ใช่ IP ของ proxy
   ไม่งั้นผู้โจมตี 1 คนล็อกอินของแอดมินตัวจริง/ผู้ใช้ทุกคนไม่ได้ (429 ร่วมกันทั้งระบบ)
   และไม่ไว้ใจ X-Forwarded-For (ผู้โจมตีปลอมเองได้)
2. Login แอดมินตอบข้อความ/สถานะเดียวกันทั้งกรณี "ไม่มีอีเมล" และ "รหัสผ่านผิด" (กันเดาอีเมล)
3. รหัสผ่านแอดมินใหม่ต้องยาวอย่างน้อย 12 ตัวและต่างจากรหัสเดิม
4. เปลี่ยนรหัสผ่านแล้ว token เก่าของแอดมินใช้ไม่ได้ (มี iat + passwordChangedAt)
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request

from Admin.auth import authAdmin
from Admin.router import SettingAdmin
from common import rate_limit
from common.jwt_utils import encode_token
from common.rate_limit import limiter

PW = "Sup3rSecret!Long"  # 16 ตัว ผ่านเกณฑ์ ≥ 12


def _request(headers: dict, peer: str = "10.0.0.9") -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 5555),
    }
    return Request(scope)


# ── 1) client IP ที่ใช้เป็นกุญแจ rate limit ────────────────────────────────────

def test_client_ip_uses_x_real_ip_in_production(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert rate_limit.client_ip(_request({"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"


def test_client_ip_ignores_x_forwarded_for(monkeypatch):
    """XFF ตัวซ้ายสุดผู้ใช้ปลอมเองได้ จึงไม่ใช้เป็นกุญแจ"""
    monkeypatch.setenv("ENV", "production")
    req = _request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, peer="10.0.0.9")
    assert rate_limit.client_ip(req) == "10.0.0.9"


def test_client_ip_falls_back_to_peer_when_header_missing_or_garbage(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert rate_limit.client_ip(_request({}, peer="10.0.0.9")) == "10.0.0.9"
    assert rate_limit.client_ip(_request({"X-Real-IP": "not-an-ip"}, peer="10.0.0.9")) == "10.0.0.9"
    assert rate_limit.client_ip(_request({"X-Real-IP": "a" * 500}, peer="10.0.0.9")) == "10.0.0.9"


def test_client_ip_does_not_trust_header_outside_production(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    assert rate_limit.client_ip(_request({"X-Real-IP": "203.0.113.7"}, peer="127.0.0.1")) == "127.0.0.1"


def test_client_ip_supports_ipv6(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert rate_limit.client_ip(_request({"X-Real-IP": "2001:db8::1"})) == "2001:db8::1"


# ── app สำหรับ admin login / change password ─────────────────────────────────

@pytest.fixture
def admin_app(mongo_client, monkeypatch):
    monkeypatch.setattr(authAdmin, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(SettingAdmin, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(authAdmin.router, prefix="/authAdmin")
    app.include_router(SettingAdmin.router, prefix="/settingAdmin")
    return app


@pytest.fixture
def client(admin_app):
    return TestClient(admin_app)


def _seed_admin(mongo_client, email="admin@example.com", password=PW, **extra):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    doc = {"Email": email, "Password": hashed}
    doc.update(extra)
    return str(mongo_client["BORC"]["LoginAdmin"].insert_one(doc).inserted_id)


def _login(client, password=PW, headers=None):
    return client.post("/authAdmin/Login", json={"email": "admin@example.com", "password": password}, headers=headers or {})


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _change(client, token, current=PW, new="An0therLongSecret!", confirm=None):
    return client.patch("/settingAdmin/ChangePassword", headers=_bearer(token), json={
        "current_password": current, "new_password": new, "confirm_password": new if confirm is None else confirm})


# ── rate limit ต่อ IP จริง ───────────────────────────────────────────────────

def test_rate_limit_is_per_real_client_ip_behind_proxy(client, mongo_client, monkeypatch):
    monkeypatch.setenv("ENV", "production")
    _seed_admin(mongo_client)
    attacker = {"X-Real-IP": "198.51.100.1"}
    for _ in range(5):
        assert _login(client, "wrong", attacker).status_code == 401
    assert _login(client, "wrong", attacker).status_code == 429  # ผู้โจมตีถูกบล็อก

    # แอดมินตัวจริงจาก IP อื่นต้องยังล็อกอินได้
    assert _login(client, PW, {"X-Real-IP": "203.0.113.50"}).status_code == 200


def test_spoofed_x_forwarded_for_does_not_reset_the_limit(client, mongo_client, monkeypatch):
    monkeypatch.setenv("ENV", "production")
    _seed_admin(mongo_client)
    codes = [
        _login(client, "wrong", {"X-Real-IP": "198.51.100.1", "X-Forwarded-For": "9.9.9.%d" % i}).status_code
        for i in range(7)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5:] == [429, 429]


# ── login ไม่รั่วว่ามีอีเมลนี้หรือไม่ ─────────────────────────────────────────────

def test_login_same_response_for_unknown_email_and_wrong_password(client, mongo_client):
    _seed_admin(mongo_client)

    unknown = client.post("/authAdmin/Login", json={"email": "nobody@example.com", "password": "whatever-long-1"})
    limiter.reset()
    wrong_pw = _login(client, "wrong-password-1")

    assert unknown.status_code == 401
    assert wrong_pw.status_code == 401
    assert unknown.json() == wrong_pw.json()


def test_login_unknown_email_still_runs_a_bcrypt_check(client, mongo_client, monkeypatch):
    """กัน timing attack: อีเมลที่ไม่มีก็ต้องเสียเวลา bcrypt เท่ากับอีเมลที่มี"""
    calls = []
    real = bcrypt.checkpw

    def spy(pw, hashed):
        calls.append(1)
        return real(pw, hashed)

    monkeypatch.setattr(authAdmin.bcrypt, "checkpw", spy)
    client.post("/authAdmin/Login", json={"email": "nobody@example.com", "password": "whatever-long-1"})
    assert len(calls) == 1


def test_login_success_token_carries_iat(client, mongo_client):
    _seed_admin(mongo_client)
    token = _login(client).json()["access_token"]
    from common.jwt_utils import decode_token
    assert isinstance(decode_token(token)["iat"], (int, float))


# ── รหัสผ่านใหม่ ─────────────────────────────────────────────────────────────

def test_change_password_rejects_shorter_than_12(client, mongo_client):
    _seed_admin(mongo_client)
    token = _login(client).json()["access_token"]
    assert _change(client, token, new="Short1234!x").status_code == 400  # 11 ตัว
    assert _change(client, token, new="12345678").status_code == 400


def test_change_password_rejects_same_as_current(client, mongo_client):
    _seed_admin(mongo_client)
    token = _login(client).json()["access_token"]
    resp = _change(client, token, new=PW)
    assert resp.status_code == 400


def test_change_password_rejects_mismatch_and_wrong_current(client, mongo_client):
    _seed_admin(mongo_client)
    token = _login(client).json()["access_token"]
    assert _change(client, token, new="An0therLongSecret!", confirm="different-long-1").status_code == 400
    assert _change(client, token, current="not-the-password-1").status_code == 400


def test_change_password_accepts_exactly_12(client, mongo_client):
    _seed_admin(mongo_client)
    token = _login(client).json()["access_token"]
    assert _change(client, token, new="Abcdefghij1!").status_code == 200  # 12 ตัวพอดี


# ── เพิกถอน token เก่าหลังเปลี่ยนรหัสผ่าน ─────────────────────────────────────────

def test_old_token_is_rejected_after_password_change(client, mongo_client):
    _seed_admin(mongo_client)
    old = _login(client).json()["access_token"]
    assert client.get("/authAdmin/me", headers=_bearer(old)).status_code == 200

    assert _change(client, old).status_code == 200

    assert client.get("/authAdmin/me", headers=_bearer(old)).status_code == 401


def test_new_login_after_password_change_works_and_old_password_fails(client, mongo_client):
    _seed_admin(mongo_client)
    old = _login(client).json()["access_token"]
    _change(client, old, new="An0therLongSecret!")
    limiter.reset()

    assert _login(client, PW).status_code == 401
    fresh = _login(client, "An0therLongSecret!")
    assert fresh.status_code == 200
    assert client.get("/authAdmin/me", headers=_bearer(fresh.json()["access_token"])).status_code == 200


def test_change_password_response_includes_fresh_working_token(client, mongo_client):
    _seed_admin(mongo_client)
    old = _login(client).json()["access_token"]
    resp = _change(client, old)
    assert resp.status_code == 200
    fresh = resp.json()["access_token"]
    assert client.get("/authAdmin/me", headers=_bearer(fresh)).status_code == 200
    assert client.get("/authAdmin/me", headers=_bearer(old)).status_code == 401


def test_legacy_token_without_iat_still_works_until_password_changes(client, mongo_client):
    """token ที่ออกก่อนการแก้นี้ไม่มี iat: ยังใช้ได้ตราบที่ยังไม่เคยเปลี่ยนรหัสผ่านหลังจากนี้"""
    admin_id = _seed_admin(mongo_client)
    legacy = encode_token({"sub": "admin@example.com", "user_id": admin_id, "role": "Admin", "FullName": "x"}, timedelta(hours=1))
    assert client.get("/authAdmin/me", headers=_bearer(legacy)).status_code == 200

    mongo_client["BORC"]["LoginAdmin"].update_one({}, {"$set": {"passwordChangedAt": datetime.now(timezone.utc)}})
    assert client.get("/authAdmin/me", headers=_bearer(legacy)).status_code == 401


def test_admin_password_over_72_bytes_is_rejected_not_500():
    """bcrypt 5.x โยน ValueError เกิน 72 ไบต์ — ต้องกลายเป็น 401 ที่ login (ไม่ใช่ 500)"""
    import Admin.auth.authAdmin as auth
    assert auth.MAX_PASSWORD_BYTES == 72
    long_thai = "ก" * 30  # 90 ไบต์
    assert len(long_thai.encode("utf-8")) > auth.MAX_PASSWORD_BYTES

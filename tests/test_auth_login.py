"""
Basic endpoint tests สำหรับ Admin login (/authAdmin/Login) และ User/LINE login
(/authUser/Login) — success + failure — ใช้ mongomock แทน MongoDB จริง และ
mock การเรียก LINE OAuth API ภายนอก
"""

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Admin.auth import authAdmin
from common.rate_limit import limiter


# ── Admin login ──────────────────────────────────────────────────────────────

@pytest.fixture
def admin_app(mongo_client, monkeypatch):
    monkeypatch.setattr(authAdmin, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    if limiter is not None:
        app.state.limiter = limiter
    app.include_router(authAdmin.router, prefix="/authAdmin")
    return app


@pytest.fixture
def admin_client(admin_app):
    return TestClient(admin_app)


def _seed_admin(mongo_client, email="admin@example.com", password="Sup3rSecret!"):
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    mongo_client["BORC"]["LoginAdmin"].insert_one({"Email": email, "Password": hashed})


def test_admin_login_success(admin_client, mongo_client):
    _seed_admin(mongo_client, "admin@example.com", "Sup3rSecret!")

    resp = admin_client.post(
        "/authAdmin/Login",
        json={"email": "admin@example.com", "password": "Sup3rSecret!"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body and body["access_token"]


def test_admin_login_wrong_password(admin_client, mongo_client):
    _seed_admin(mongo_client, "admin@example.com", "Sup3rSecret!")

    resp = admin_client.post(
        "/authAdmin/Login",
        json={"email": "admin@example.com", "password": "wrong-password"},
    )

    assert resp.status_code == 401


def test_admin_login_unknown_email(admin_client, mongo_client):
    resp = admin_client.post(
        "/authAdmin/Login",
        json={"email": "nobody@example.com", "password": "whatever"},
    )

    assert resp.status_code == 404


def test_admin_login_missing_fields(admin_client):
    resp = admin_client.post("/authAdmin/Login", json={"email": "", "password": ""})
    assert resp.status_code == 400


def test_admin_me_requires_token(admin_client):
    resp = admin_client.get("/authAdmin/me")
    # ไม่มี Authorization header -> FastAPI OAuth2PasswordBearer ปฏิเสธด้วย 401
    assert resp.status_code == 401


# ── User / LINE login ────────────────────────────────────────────────────────

@pytest.fixture
def user_app(mongo_client, monkeypatch):
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    if limiter is not None:
        app.state.limiter = limiter
    app.include_router(authUser.router, prefix="/authUser")
    return app, authUser


@pytest.fixture
def user_client(user_app):
    app, _ = user_app
    return TestClient(app)


def test_line_login_new_user_gets_registration_token(user_app, user_client, monkeypatch):
    app, authUser = user_app

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {"userId": "line-uid-1", "pictureUrl": "https://example.com/p.jpg"},
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["lineUserId"] == "line-uid-1"
    assert body["Role"] is None
    # ต้องออก HttpOnly session cookie ให้ผู้ใช้ใหม่ไปกรอกโปรไฟล์ต่อ
    assert "access_token" in resp.cookies


def test_line_login_existing_approved_user_success(user_app, user_client, mongo_client, monkeypatch):
    app, authUser = user_app

    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": "line-uid-2",
        "Role": "Student",
        "Status": "Approved",
        "Prefix": "นาย",
        "Firstname": "ทดสอบ",
        "Lastname": "ระบบ",
        "imageURL": "",
    })

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {"userId": "line-uid-2", "pictureUrl": ""},
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == "line-uid-2"
    assert body["Role"] == "Student"
    assert body["Status"] == "Approved"


def test_line_login_suspended_user_is_forbidden(user_app, user_client, mongo_client, monkeypatch):
    app, authUser = user_app

    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": "line-uid-3",
        "Role": "Student",
        "Status": "Suspended",
    })

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {"userId": "line-uid-3", "pictureUrl": ""},
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 403


def test_exchange_code_logs_line_error_body_on_non_200(monkeypatch, caplog):
    """Regression: เดิม exchange_code_for_access_token ไม่ log อะไรเลยเมื่อ LINE ตอบ
    status != 200 (log เฉพาะกรณี network error) ทำให้ debug ไม่ได้ว่า LINE ปฏิเสธเพราะอะไร
    (เช่น redirect_uri ไม่ตรง / code ถูกใช้ไปแล้ว) — ตอนนี้ต้อง log status + response body"""
    from fastapi import HTTPException
    from users.auth import authUser

    class _FakeResponse:
        status_code = 400
        text = '{"error":"invalid_grant","error_description":"redirect_uri mismatch"}'

    monkeypatch.setattr(authUser.requests, "post", lambda *a, **kw: _FakeResponse())

    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException) as exc_info:
            authUser.exchange_code_for_access_token("some-code")

    assert exc_info.value.status_code == 401
    assert any(
        "400" in record.message and "redirect_uri mismatch" in record.message
        for record in caplog.records
    )


def test_line_login_bad_auth_code_returns_401(user_app, user_client, monkeypatch):
    app, authUser = user_app

    from fastapi import HTTPException, status

    def _raise_invalid_code(code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization Code ไม่ถูกต้อง หรือหมดอายุ กรุณาลองเข้าสู่ระบบใหม่อีกครั้ง",
        )

    monkeypatch.setattr(authUser, "exchange_code_for_access_token", _raise_invalid_code)

    resp = user_client.post("/authUser/Login", json={"code": "invalid-code"})

    assert resp.status_code == 401


def test_line_login_pending_user_gets_session_but_status_pending(
    user_app, user_client, mongo_client, monkeypatch
):
    """ผู้ใช้เดิมที่ Status ยังเป็น Pending (ไม่ใช่ BLOCKED_STATUSES) ต้อง login ผ่านได้
    (ได้ session cookie) แต่ response ต้องบอก Status: Pending ให้ frontend พาไปหน้า
    WaitingApproval — ไม่ใช่ error"""
    app, authUser = user_app

    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": "line-uid-pending",
        "Role": "Student",
        "Status": "Pending",
        "Prefix": "นาย",
        "Firstname": "รอ",
        "Lastname": "อนุมัติ",
        "imageURL": "",
    })

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {"userId": "line-uid-pending", "pictureUrl": ""},
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["Status"] == "Pending"
    assert "access_token" in resp.cookies


def test_line_login_missing_user_id_returns_400(user_app, user_client, monkeypatch):
    """LINE profile response ไม่มี userId -> 400 (ไม่ใช่ 500/crash)"""
    app, authUser = user_app

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser, "verify_line_token", lambda access_token: {"pictureUrl": "https://x/p.jpg"}
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 400


def test_line_login_updates_placeholder_image_from_line_profile(
    user_app, user_client, mongo_client, monkeypatch
):
    """ถ้า imageURL ในระบบยังเป็นค่าว่าง/placeholder ('line') ต้องอัปเดตเป็นรูปจาก LINE
    profile ปัจจุบัน แต่ถ้าผู้ใช้เคยตั้งรูปเองแล้ว (URL อื่น) ต้องไม่ถูกเขียนทับ"""
    app, authUser = user_app

    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": "line-uid-4",
        "Role": "Student",
        "Status": "Approved",
        "Prefix": "นาย",
        "Firstname": "ทดสอบ",
        "Lastname": "รูปภาพ",
        "imageURL": "line",
    })

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {
            "userId": "line-uid-4",
            "pictureUrl": "https://profile.line-scdn.net/new-pic.jpg",
        },
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 200
    assert resp.json()["ImageUrl"] == "https://profile.line-scdn.net/new-pic.jpg"

    updated = mongo_client["BORC"]["UserProfile"].find_one({"userId": "line-uid-4"})
    assert updated["imageURL"] == "https://profile.line-scdn.net/new-pic.jpg"


def test_line_login_keeps_custom_image_set_by_user(
    user_app, user_client, mongo_client, monkeypatch
):
    app, authUser = user_app

    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": "line-uid-5",
        "Role": "Student",
        "Status": "Approved",
        "Prefix": "นาย",
        "Firstname": "ทดสอบ",
        "Lastname": "ตั้งรูปเอง",
        "imageURL": "https://cloudinary.example/custom-avatar.jpg",
    })

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {
            "userId": "line-uid-5",
            "pictureUrl": "https://profile.line-scdn.net/new-pic.jpg",
        },
    )

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 200
    assert resp.json()["ImageUrl"] == "https://cloudinary.example/custom-avatar.jpg"


def test_line_login_unhandled_error_returns_generic_500(user_app, user_client, monkeypatch):
    """Regression: Line_Login เดิมไม่มี try/except ครอบทั้ง endpoint (ไม่เหมือน
    authAdmin.py's /Login) — unexpected exception (เช่น DB error) ต้องถูกจับแล้วตอบ
    generic 500 พร้อม log แทนที่จะหลุดออกไปแบบ unhandled"""
    app, authUser = user_app

    monkeypatch.setattr(
        authUser, "exchange_code_for_access_token", lambda code: "fake-line-access-token"
    )
    monkeypatch.setattr(
        authUser,
        "verify_line_token",
        lambda access_token: {"userId": "line-uid-6", "pictureUrl": ""},
    )

    def _boom(uuid):
        raise RuntimeError("mongo connection refused")

    monkeypatch.setattr(authUser, "get_UserDB", _boom)

    resp = user_client.post("/authUser/Login", json={"code": "fake-auth-code"})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal server error"
    assert "mongo connection refused" not in resp.text

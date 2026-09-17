"""
Tests สำหรับ SetupProfile.py (/SetupProfile/AddDataProfile) — หน้ากรอกโปรไฟล์
ครั้งแรกหลัง LINE login

Regression: frontend เดิมเรียกด้วย PATCH แต่ route ถูก register ไว้เป็น POST
เท่านั้น -> 405 Method Not Allowed เสมอ (ดู src/lib/api/auth.ts,
src/routes/auth/ProfileSetup.tsx ฝั่ง frontend)
"""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token
from users.router import SetupProfile

COOKIE_NAME = "access_token"


@pytest.fixture
def app(mongo_client, monkeypatch):
    monkeypatch.setattr(SetupProfile, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.include_router(SetupProfile.router, prefix="/SetupProfile")
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _registration_cookie(user_id: str) -> str:
    return encode_token(
        {"user_id": user_id, "registration": True},
        timedelta(minutes=15),
    )


VALID_PAYLOAD = {
    "prefix": "นาย",
    "firstname": "ทดสอบ",
    "lastname": "ระบบ",
    "faculty": "วิทยาศาสตร์",
    "department": "วิทยาการคอมพิวเตอร์และปัญญาประดิษฐ์",
    "userID": "line-uid-new",
    "imageURL": "https://profile.line-scdn.net/pic.jpg",
}


def test_add_data_profile_post_succeeds(client):
    """เส้นทางที่ถูกต้อง: POST พร้อม field ตรงกับ DataProfile model ต้องสำเร็จและ
    บันทึกลง DB ด้วยชื่อ field ที่ตรงกับ schema เดิม (Prefix/Firstname/... ตัวใหญ่)"""
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-new"))

    resp = client.post("/SetupProfile/AddDataProfile", json=VALID_PAYLOAD)

    assert resp.status_code == 200
    assert resp.json() == {"message": "เพิ่มข้อมูลผู้ใช้ใหม่เสร็จสิ้น"}
    assert "access_token" in resp.cookies


def test_add_data_profile_persists_matching_database_shape(client, mongo_client):
    """ตรวจว่าข้อมูลที่บันทึกจริงใน MongoDB ตรงกับ field/รูปแบบที่ระบบอื่น (login,
    WaitingApproval) คาดหวัง เช่น key เป็น 'userId' (ตัว d เล็ก) ไม่ใช่ 'userID'"""
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-shape"))

    payload = {**VALID_PAYLOAD, "userID": "line-uid-shape"}
    resp = client.post("/SetupProfile/AddDataProfile", json=payload)
    assert resp.status_code == 200

    saved = mongo_client["BORC"]["UserProfile"].find_one({"userId": "line-uid-shape"})
    assert saved is not None
    assert saved["Prefix"] == "นาย"
    assert saved["Firstname"] == "ทดสอบ"
    assert saved["Lastname"] == "ระบบ"
    # Role ต้องว่างเสมอตอนสมัครเอง — ห้าม client กำหนด role ให้ตัวเอง (ดู Admin
    # เท่านั้นที่กำหนด role ได้ผ่าน ManagementAccount.UpdateAccountUser)
    assert saved["Role"] == ""
    assert saved["Status"] == "Pending"
    assert saved["imageURL"] == "https://profile.line-scdn.net/pic.jpg"


def test_add_data_profile_ignores_client_supplied_role(client, mongo_client):
    """แม้ client จะยัด field 'role' เข้ามาในเพย์โหลด (ผ่าน extra field) ก็ต้องถูก
    เพิกเฉย ห้ามให้ผู้สมัครตั้ง role ให้ตัวเองได้เด็ดขาด."""
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-role-hack"))

    resp = client.post(
        "/SetupProfile/AddDataProfile",
        json={**VALID_PAYLOAD, "userID": "line-uid-role-hack", "role": "Advisor"},
    )

    assert resp.status_code == 200
    saved = mongo_client["BORC"]["UserProfile"].find_one({"userId": "line-uid-role-hack"})
    assert saved["Role"] == ""


def test_add_data_profile_rejects_unknown_faculty(client):
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-bad-faculty"))

    resp = client.post(
        "/SetupProfile/AddDataProfile",
        json={**VALID_PAYLOAD, "userID": "line-uid-bad-faculty", "faculty": "ไม่มีคณะนี้"},
    )
    assert resp.status_code == 422


def test_add_data_profile_rejects_department_not_in_faculty(client):
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-bad-dept"))

    resp = client.post(
        "/SetupProfile/AddDataProfile",
        json={
            **VALID_PAYLOAD,
            "userID": "line-uid-bad-dept",
            "department": "สาขาที่ไม่มีอยู่จริง",
        },
    )

    assert resp.status_code == 422


def test_add_data_profile_rejects_patch_method(client):
    """Regression: เดิม frontend เรียกด้วย sessionApi.patch(...) แต่ route มีแค่ POST
    -> ต้องได้ 405 Method Not Allowed (สาเหตุของบั๊กที่รายงานมา)"""
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-new"))

    resp = client.patch("/SetupProfile/AddDataProfile", json=VALID_PAYLOAD)

    assert resp.status_code == 405


def test_add_data_profile_rejects_capitalized_field_names(client):
    """Regression: เดิม frontend ส่ง key ตัวพิมพ์ใหญ่ (Prefix/Firstname/Lastname/Role)
    และไม่มี userID เลย ซึ่งไม่ตรงกับ DataProfile pydantic model (lowercase, userID
    required) -> ต้องได้ 422 Unprocessable Entity ไม่ใช่ 200"""
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-new"))

    old_shape_payload = {
        "Prefix": "นาย",
        "Firstname": "ทดสอบ",
        "Lastname": "ระบบ",
        "Role": "Student",
    }

    resp = client.post("/SetupProfile/AddDataProfile", json=old_shape_payload)

    assert resp.status_code == 422


def test_add_data_profile_requires_matching_session_user_id(client):
    """userID ใน payload ต้องตรงกับ user_id ใน registration token — ป้องกันคนหนึ่ง
    สมัครโปรไฟล์แทนอีกคนหนึ่ง"""
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-A"))

    resp = client.post(
        "/SetupProfile/AddDataProfile",
        json={**VALID_PAYLOAD, "userID": "line-uid-B"},
    )

    assert resp.status_code == 403


def test_add_data_profile_without_session_cookie_is_unauthorized(client):
    resp = client.post("/SetupProfile/AddDataProfile", json=VALID_PAYLOAD)
    assert resp.status_code == 401


def test_add_data_profile_duplicate_user_returns_conflict(client, mongo_client):
    mongo_client["BORC"]["UserProfile"].insert_one({"userId": "line-uid-dup"})
    client.cookies.set(COOKIE_NAME, _registration_cookie("line-uid-dup"))

    resp = client.post(
        "/SetupProfile/AddDataProfile",
        json={**VALID_PAYLOAD, "userID": "line-uid-dup"},
    )

    assert resp.status_code == 409

"""
Tests สำหรับ Admin/router/GetProfileUser.py

Regression: PATCH /Profile/{user_id} (Admin ยืนยันสิทธิ์/แก้ไขบัญชี) เดิมบันทึก
role ลง AccountManagementHistory ผิด — ใช้ Role "เก่า" (ก่อนอัปเดต) แทนที่จะใช้
Role "ใหม่" ที่เพิ่งเขียนลง DB ทำให้หน้า "ประวัติการจัดการบัญชี" ไม่มี role ให้แสดง
เพราะผู้ใช้ที่เพิ่งสมัคร (Pending) มี Role="" อยู่แล้วเป็นทุนเดิม (key มีอยู่จริง
ไม่ใช่ None) `dict.get("Role", update.Role)` จึงไม่เคย fallback ไปใช้ default เลย
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Admin.router import GetProfileUser


@pytest.fixture
def app(mongo_client, monkeypatch):
    monkeypatch.setattr(GetProfileUser, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.include_router(GetProfileUser.router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_approve_pending_user_saves_new_role_to_history(client, mongo_client):
    """เคสหลักที่ user รายงานมา: user สมัครเองแล้ว Role="" ตั้งแต่ตอนสมัคร (Pending)
    -> Admin เลือก Role='Advisor' แล้วกดยืนยันสิทธิ์ -> history ต้องบันทึก 'Advisor'
    ไม่ใช่ '' (ค่าเก่าก่อนอัปเดต)"""
    db = mongo_client["BORC"]
    db["UserProfile"].insert_one({
        "userId": "line-uid-1",
        "Firstname": "ทดสอบ",
        "Lastname": "ระบบ",
        "Role": "",
        "Status": "Pending",
    })

    resp = client.patch(
        "/Profile/line-uid-1",
        json={"Role": "Advisor", "Status": "Approved"},
    )

    assert resp.status_code == 200
    assert resp.json()["Role"] == "Advisor"

    history = list(db["AccountManagementHistory"].find({}, {"_id": 0}))
    assert len(history) == 1
    assert history[0]["role"] == "Advisor"
    assert history[0]["statusLabel"] == "ยืนยันสิทธิ์แล้ว"


def test_change_role_of_existing_approved_user_updates_history(client, mongo_client):
    """เคสแก้ไข role ของ user ที่ Approved อยู่แล้ว (เดิมมี Role='Student') ->
    history ต้องบันทึก role ใหม่ ('Advisor') ไม่ใช่ role เก่า"""
    db = mongo_client["BORC"]
    db["UserProfile"].insert_one({
        "userId": "line-uid-2",
        "Firstname": "สมชาย",
        "Lastname": "ใจดี",
        "Role": "Student",
        "Status": "Approved",
    })

    resp = client.patch(
        "/Profile/line-uid-2",
        json={"Role": "Advisor", "Status": "Approved"},
    )

    assert resp.status_code == 200

    history = list(db["AccountManagementHistory"].find({}, {"_id": 0}))
    assert history[0]["role"] == "Advisor"


def test_update_role_user_not_found_returns_404(client):
    resp = client.patch(
        "/Profile/does-not-exist",
        json={"Role": "Student", "Status": "Approved"},
    )
    assert resp.status_code == 404

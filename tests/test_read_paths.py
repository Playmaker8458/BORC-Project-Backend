"""
Characterization + regression tests สำหรับ read path ที่ถูกปรับให้ลดจำนวนรอบ DB:

- Students/QueuehistoryStudent.py  GET /History
- Students/Show_BookingData.py     GET /BookingStats
- Advisor/Show_Consult.py          GET /AdvisorStats
- auth/authUser.py                 GET /Me, /NavbarUsers, /Student/dashboard, /Advisor/dashboard

ผลลัพธ์ (JSON + ลำดับ) ต้องเหมือนเดิมทุกตัวอักษร นอกจากนี้ตรวจว่า cache ข้อมูลผู้ใช้ (15 วินาที)
ถูกล้างทันทีเมื่อ Admin/ผู้ใช้แก้ไขบัญชี จึงไม่มีสิทธิ์/ชื่อเก่าค้าง
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token


def _cookies(user_id):
    return {"access_token": encode_token({"user_id": user_id}, timedelta(days=1))}


def _seed_user(mongo_client, user_id, role="Student", status="Approved", **extra):
    doc = {"userId": user_id, "Role": role, "Status": status, "Prefix": "นาย",
           "Firstname": "ทดสอบ", "Lastname": "ระบบ", "imageURL": ""}
    doc.update(extra)
    mongo_client["BORC"]["UserProfile"].insert_one(doc)


T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _t(minutes):
    return T0 + timedelta(minutes=minutes)


# ── History ───────────────────────────────────────────────────────────────────

@pytest.fixture
def qhs_client(mongo_client, monkeypatch):
    from users.router.Students import QueuehistoryStudent as qhs
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(qhs, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.include_router(qhs.router)
    return TestClient(app)


def test_history_exact_output_and_order(qhs_client, mongo_client):
    """ผลรวม 4 แหล่ง เรียง createdAt ใหม่->เก่า; createdAt เท่ากันต้องคงลำดับ Approved, Reschedule, Cancel, Completed"""
    db = mongo_client["BORC"]
    _seed_user(mongo_client, "student-1")
    db["ApprovedHistory"].insert_many([
        {"UserId": "student-1", "AdvisorName": "อ.เอ", "Status": "Approved", "CreatedAt": _t(5), "UpdatedAt": _t(5)},
        {"UserId": "other", "AdvisorName": "อ.ซี", "Status": "Approved", "CreatedAt": _t(99)},
    ])
    db["RescheduleHistory"].insert_many([
        {"rescheduledById": "student-1", "advisorName": "อ.บี", "rescheduledByRole": "Student",
         "status": "Rescheduled", "rescheduledReason": "r1", "createdAt": _t(5), "updatedAt": _t(5)},
        {"studentId": "student-1", "rescheduledById": "adv", "advisorName": "อ.บี", "rescheduledByRole": "Advisor",
         "status": "Rescheduled", "rescheduledReason": "r2", "createdAt": _t(1), "updatedAt": _t(1)},
        {"studentId": "nobody", "rescheduledById": "adv", "createdAt": _t(50)},
    ])
    db["CancelBookingHistory"].insert_many([
        {"cancelledById": "student-1", "advisorName": "อ.ดี", "cancelledByRole": "Student",
         "status": "Cancelled", "cancelReason": "c1", "createdAt": _t(5), "updatedAt": _t(5)},
        {"cancelledById": "other", "createdAt": _t(60)},
    ])
    db["QueueManagementHistory"].insert_many([
        {"userId": "student-1", "status": "Completed", "UserName": "อ.อี", "role": "Advisor",
         "Reason": "done", "createdAt": _t(9), "updatedAt": _t(9)},
        {"userId": "student-1", "status": "Approved", "createdAt": _t(70)},  # ไม่ใช่ Completed ต้องไม่ถูกนับ
    ])

    resp = qhs_client.get("/History", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [(d["UserName"], d["role"], d["status"], d["Reason"]) for d in data] == [
        ("อ.อี", "Advisor", "Completed", "done"),        # t=9
        ("อ.เอ", "Advisor", "Approved", None),           # t=5 (approved ก่อน)
        ("อ.บี", "Student", "Rescheduled", "r1"),        # t=5
        ("อ.ดี", "Student", "Cancelled", "c1"),          # t=5
        ("อ.บี", "Advisor", "Rescheduled", "r2"),        # t=1
    ]


def test_history_empty_for_new_user(qhs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    resp = qhs_client.get("/History", cookies=_cookies("student-1"))
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


# ── BookingStats ──────────────────────────────────────────────────────────────

@pytest.fixture
def sbd_client(mongo_client, monkeypatch):
    from users.router.Students import Show_BookingData as sbd
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(sbd, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.include_router(sbd.router)
    return TestClient(app)


def test_booking_stats_counts_exact(sbd_client, mongo_client):
    db = mongo_client["BORC"]
    _seed_user(mongo_client, "student-1")
    db["RescheduleHistory"].insert_many([
        {"rescheduledById": "student-1", "rescheduledByRole": "Student"},
        {"rescheduledById": "student-1", "rescheduledByRole": "Student"},
        {"rescheduledById": "student-1", "rescheduledByRole": "Advisor"},  # ไม่นับ
        {"rescheduledById": "other", "rescheduledByRole": "Student"},      # ไม่นับ
    ])
    db["CancelBookingHistory"].insert_many([
        {"cancelledById": "student-1", "cancelledByRole": "Student"},
        {"cancelledById": "student-1", "cancelledByRole": "Advisor"},      # ไม่นับ
    ])

    resp = sbd_client.get("/BookingStats", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == {"stats": {"rescheduled": 2, "cancelled": 1}}


def test_booking_stats_zero_for_new_user(sbd_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    resp = sbd_client.get("/BookingStats", cookies=_cookies("student-1"))
    assert resp.json() == {"stats": {"rescheduled": 0, "cancelled": 0}}


# ── AdvisorStats ──────────────────────────────────────────────────────────────

@pytest.fixture
def sc_client(mongo_client, monkeypatch):
    from users.router.Advisor import Show_Consult as sc
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(sc, "Connect_MongoDB", lambda: mongo_client)
    app = FastAPI()
    app.include_router(sc.router)
    return TestClient(app)


def test_advisor_stats_counts_exact(sc_client, mongo_client):
    """นับ Approved/Completed ด้วย advisorId; เลื่อน/ยกเลิก ด้วยชื่อ "{Prefix}{Firstname} {Lastname}" (พฤติกรรมเดิม)"""
    db = mongo_client["BORC"]
    _seed_user(mongo_client, "advisor-1", role="Advisor", Prefix="ผศ.", Firstname="เอ", Lastname="บี")
    name = "ผศ.เอ บี"
    db["ApprovedHistory"].insert_many([
        {"AdvisorId": "advisor-1", "Status": "Approved"},
        {"AdvisorId": "advisor-1", "Status": "Approved"},
        {"AdvisorId": "advisor-1", "Status": "Other"},     # ไม่นับ
        {"AdvisorId": "advisor-2", "Status": "Approved"},  # ไม่นับ
    ])
    db["RescheduleHistory"].insert_many([{"advisorName": name}, {"advisorName": name}, {"advisorName": "ชื่ออื่น"}])
    db["CancelBookingHistory"].insert_many([{"advisorName": name}, {"advisorName": "ชื่ออื่น"}])
    db["QueueManagementHistory"].insert_many([
        {"userId": "advisor-1", "status": "Completed"},
        {"userId": "advisor-1", "status": "Approved"},     # ไม่นับ
        {"userId": "advisor-2", "status": "Completed"},    # ไม่นับ
    ])

    resp = sc_client.get("/AdvisorStats", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    assert resp.json() == {"stats": {"Approved": 2, "rescheduled": 2, "cancelled": 1, "completed": 1}}


def test_advisor_stats_zero_for_new_advisor(sc_client, mongo_client):
    _seed_user(mongo_client, "advisor-1", role="Advisor")
    resp = sc_client.get("/AdvisorStats", cookies=_cookies("advisor-1"))
    assert resp.json() == {"stats": {"Approved": 0, "rescheduled": 0, "cancelled": 0, "completed": 0}}


def test_advisor_stats_rejects_student(sc_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    # พฤติกรรมเดิม: endpoint นี้ไม่ได้ตรวจ role เอง (บังคับที่ dependency ใน main.py) จึงตอบ 200
    assert sc_client.get("/AdvisorStats", cookies=_cookies("student-1")).status_code == 200


# ── authUser: Me / NavbarUsers / dashboards ──────────────────────────────────

@pytest.fixture
def auth_env(mongo_client, monkeypatch):
    from users.auth import authUser
    from users.router import SettingProfile
    from Admin.router import GetProfileUser, ManagementAccount

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(SettingProfile, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(GetProfileUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ManagementAccount, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(authUser.router, prefix="/authUser")
    app.include_router(SettingProfile.router, prefix="/settingProfile")
    app.include_router(GetProfileUser.router)
    app.include_router(ManagementAccount.router)
    return TestClient(app), mongo_client


def test_me_returns_exact_profile(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1", Prefix="นาย", Firstname="เอ", Lastname="บี", imageURL="http://img/x.png")

    resp = client.get("/authUser/Me", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "student-1", "role": "Student", "status": "Approved",
                           "Prefix": "นาย", "Firstname": "เอ", "Lastname": "บี", "ImageUrl": "http://img/x.png"}


def test_me_missing_fields_default_to_empty_string(auth_env):
    client, mongo = auth_env
    mongo["BORC"]["UserProfile"].insert_one({"userId": "student-1", "Role": "Student", "Status": "Approved"})

    resp = client.get("/authUser/Me", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "student-1", "role": "Student", "status": "Approved",
                           "Prefix": "", "Firstname": "", "Lastname": "", "ImageUrl": ""}


def test_me_requires_cookie(auth_env):
    client, _ = auth_env
    assert client.get("/authUser/Me").status_code == 401


def test_me_unknown_user_is_401(auth_env):
    client, _ = auth_env
    assert client.get("/authUser/Me", cookies=_cookies("ghost")).status_code == 401


@pytest.mark.parametrize("status", ["Pending", "Suspended", ""])
def test_me_unapproved_user_is_403(auth_env, status):
    client, mongo = auth_env
    _seed_user(mongo, "student-1", status=status)
    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 403


def test_student_dashboard_message_and_role(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1", Prefix="นาย", Firstname="เอ", Lastname="บี")
    resp = client.get("/authUser/Student/dashboard", cookies=_cookies("student-1"))
    assert resp.status_code == 200
    assert resp.json() == {"message": "นายเอ บี", "role": "Student"}


def test_student_dashboard_rejects_advisor(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "advisor-1", role="Advisor")
    assert client.get("/authUser/Student/dashboard", cookies=_cookies("advisor-1")).status_code == 403


def test_advisor_dashboard_message_and_role(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "advisor-1", role="Advisor", Prefix="ผศ.", Firstname="เอ", Lastname="บี")
    resp = client.get("/authUser/Advisor/dashboard", cookies=_cookies("advisor-1"))
    assert resp.status_code == 200
    assert resp.json() == {"message": "ผศ.เอ บี", "role": "Advisor"}


def test_advisor_dashboard_rejects_student(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1")
    assert client.get("/authUser/Advisor/dashboard", cookies=_cookies("student-1")).status_code == 403


def test_navbar_users_is_always_fresh_from_db(auth_env):
    """NavbarUsers ต้องอ่าน DB สดทุกครั้ง (ไม่ผ่าน cache 15 วินาที) เพราะใช้แสดงรูปหลังอัปเดตโปรไฟล์"""
    client, mongo = auth_env
    _seed_user(mongo, "student-1", imageURL="http://img/old.png", updatedAt=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert client.get("/authUser/NavbarUsers", cookies=_cookies("student-1")).json()["ImageUrl"].startswith("http://img/old.png")

    mongo["BORC"]["UserProfile"].update_one({"userId": "student-1"}, {"$set": {"imageURL": "http://img/new.png"}})

    resp = client.get("/authUser/NavbarUsers", cookies=_cookies("student-1"))
    assert resp.json()["ImageUrl"].startswith("http://img/new.png")


# ── cache invalidation: บัญชีที่ถูกแก้ไข/ระงับ/ลบ ต้องมีผลทันที ────────────────

def test_admin_suspend_takes_effect_immediately_despite_cache(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1")
    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 200  # อุ่น cache

    resp = client.patch("/Profile/student-1", json={"Role": "Student", "Status": "Suspended"})
    assert resp.status_code == 200

    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 403


def test_admin_delete_profile_takes_effect_immediately(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1")
    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 200

    resp = client.delete("/Profile/student-1")
    assert resp.status_code == 200

    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 401


def test_admin_update_account_takes_effect_immediately(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1")
    oid = str(mongo["BORC"]["UserProfile"].find_one({"userId": "student-1"})["_id"])
    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 200

    resp = client.put("/UpdateAccountUser", json={
        "id": oid, "Prefix": "นาง", "Firstname": "ใหม่", "Lastname": "ชื่อ", "Role": "Student",
        "Status": "Suspended", "Faculty": "", "Department": "",
    })
    assert resp.status_code == 200

    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 403


def test_admin_delete_account_user_takes_effect_immediately(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1")
    oid = str(mongo["BORC"]["UserProfile"].find_one({"userId": "student-1"})["_id"])
    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 200

    resp = client.request("DELETE", "/DeleteAccountUser", json={"id": oid})
    assert resp.status_code == 200

    assert client.get("/authUser/Me", cookies=_cookies("student-1")).status_code == 401


def test_name_change_reflected_immediately_in_me(auth_env):
    client, mongo = auth_env
    _seed_user(mongo, "student-1", Prefix="นาย", Firstname="เก่า", Lastname="ชื่อ")
    assert client.get("/authUser/Me", cookies=_cookies("student-1")).json()["Firstname"] == "เก่า"

    resp = client.patch("/settingProfile/UpdateProfileName",
                        json={"Prefix": "นาย", "Firstname": "ใหม่", "Lastname": "ชื่อ"}, cookies=_cookies("student-1"))
    assert resp.status_code == 200

    assert client.get("/authUser/Me", cookies=_cookies("student-1")).json()["Firstname"] == "ใหม่"
    assert client.get("/authUser/Student/dashboard", cookies=_cookies("student-1")).json()["message"] == "นายใหม่ ชื่อ"

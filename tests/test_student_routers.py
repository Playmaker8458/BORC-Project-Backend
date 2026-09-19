"""
Endpoint coverage สำหรับ Student routers ที่ก่อนหน้านี้ไม่มี test เลย:

- ManageQueueStudent.py  (MyBookingDetail, CheckRescheduleEligibility, CancelBooking, MyCancelCount)
- Reschedule_Students.py (BookingInfo, AvailableSlots, RescheduleBooking)
- Show_BookingData.py    (ShowData, BookingStats)
- ViewConsultationHours.py (Advisors, Slots/{advisor_id})
- QueuehistoryStudent.py (History)

รูปแบบตาม tests/test_auth_login.py: mount router เดี่ยว ๆ บน FastAPI app เปล่า,
monkeypatch Connect_MongoDB ให้ชี้ไปที่ mongomock, และออก cookie ด้วย
common.jwt_utils.encode_token (payload มีแค่ user_id — role/status มาจาก
UserProfile ใน DB จริง ๆ ตามที่ verify_user_token ต้องการ).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token


# ── shared helpers ────────────────────────────────────────────────────────────

def _make_token(user_id: str) -> str:
    return encode_token({"user_id": user_id}, timedelta(days=1))


def _seed_user(mongo_client, user_id, role="Student", status="Approved", **extra):
    doc = {
        "userId": user_id,
        "Role": role,
        "Status": status,
        "Prefix": "นาย",
        "Firstname": "ทดสอบ",
        "Lastname": "ระบบ",
        "imageURL": "",
    }
    doc.update(extra)
    mongo_client["BORC"]["UserProfile"].insert_one(doc)


def _cookies(user_id: str) -> dict:
    return {"access_token": _make_token(user_id)}


# ── ManageQueueStudent ─────────────────────────────────────────────────────────

@pytest.fixture
def mqs_app(mongo_client, monkeypatch):
    from users.router.Students import ManageQueueStudent as mqs
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(mqs, "Connect_MongoDB", lambda: mongo_client)
    # BookingOnline.auto_update_status is imported by name; patch it to a no-op so
    # tests aren't coupled to that unrelated module's behaviour.
    monkeypatch.setattr(mqs, "auto_update_status", lambda db: None)

    app = FastAPI()
    app.include_router(mqs.router)
    return app, mqs


@pytest.fixture
def mqs_client(mqs_app):
    app, _ = mqs_app
    return TestClient(app)


def _insert_booking(mongo_client, **kw):
    now = datetime.now(timezone.utc)
    doc = {
        "UserId": "student-1",
        "Advisor_Name": "อ.ทดสอบ",
        "AdvisorId": "advisor-1",
        "Date": "2099-01-01",
        "Time": "09:00-10:00",
        "TimeLabel": "MORNING",
        "ResearchTopic": "Topic",
        "ResearchDetail": "Detail",
        "Status": "Pending",
        "RescheduledOnce": False,
        "StudentName": "นาย ทดสอบ ระบบ",
        "CreatedAt": now,
        "UpdatedAt": now,
    }
    doc.update(kw)
    return mongo_client["BORC"]["BookingOnline"].insert_one(doc)


def test_my_booking_detail_returns_active_booking(mqs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client)

    resp = mqs_client.get("/MyBookingDetail", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["booking"]["Advisor_Name"] == "อ.ทดสอบ"
    assert body["booking"]["Status"] == "Pending"


def test_my_booking_detail_no_active_booking(mqs_client, mongo_client):
    _seed_user(mongo_client, "student-1")

    resp = mqs_client.get("/MyBookingDetail", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == {"booking": None}


def test_my_booking_detail_requires_auth(mqs_client):
    resp = mqs_client.get("/MyBookingDetail")
    assert resp.status_code == 401


def test_cancel_booking_success_for_pending(mqs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client, Status="Pending", Date="2099-01-01", Time="09:00-10:00")

    resp = mqs_client.request(
        "DELETE", "/CancelBooking", json={"cancelReason": "เปลี่ยนใจ"}, cookies=_cookies("student-1")
    )

    assert resp.status_code == 200
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Cancelled"
    assert booking["CancelReason"] == "เปลี่ยนใจ"


def test_cancel_booking_rejects_already_approved_status(mqs_client, mongo_client):
    """CANCELLABLE_STATUSES == ["Pending"] -> Approved bookings cannot be self-cancelled."""
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client, Status="Approved", Date="2099-01-01", Time="09:00-10:00")

    resp = mqs_client.request(
        "DELETE", "/CancelBooking", json={"cancelReason": ""}, cookies=_cookies("student-1")
    )

    assert resp.status_code == 400
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Approved"


def test_cancel_booking_cannot_cancel_other_students_booking(mqs_client, mongo_client):
    """Cross-user check: student-2's cookie must not be able to touch student-1's booking."""
    _seed_user(mongo_client, "student-1")
    _seed_user(mongo_client, "student-2")
    _insert_booking(mongo_client, UserId="student-1", Status="Pending")

    resp = mqs_client.request(
        "DELETE", "/CancelBooking", json={"cancelReason": ""}, cookies=_cookies("student-2")
    )

    assert resp.status_code == 404
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Pending"


def test_my_cancel_count(mqs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    mongo_client["BORC"]["CancelBookingHistory"].insert_many([
        {"cancelledById": "student-1", "cancelledByRole": "Student"},
        {"cancelledById": "student-1", "cancelledByRole": "Student"},
        {"cancelledById": "other", "cancelledByRole": "Student"},
    ])

    resp = mqs_client.get("/MyCancelCount", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == {"self_cancel": 2}


# ── Reschedule_Students ────────────────────────────────────────────────────────

@pytest.fixture
def rs_app(mongo_client, monkeypatch):
    from users.router.Students import Reschedule_Students as rs
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(rs, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(rs.router)
    return app, rs


@pytest.fixture
def rs_client(rs_app):
    app, _ = rs_app
    return TestClient(app)


def test_booking_info_no_active_booking(rs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    resp = rs_client.get("/BookingInfo", cookies=_cookies("student-1"))
    assert resp.status_code == 200
    assert resp.json() == {"booking": None}


def test_booking_info_can_reschedule_true_when_approved_and_not_used(rs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client, Status="Approved", Date="2099-01-01", Time="09:00-10:00", RescheduledOnce=False)

    resp = rs_client.get("/BookingInfo", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json()["booking"]["can_reschedule"] is True


def test_reschedule_booking_rejects_missing_reason(rs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client, Status="Approved", RescheduledOnce=False)

    resp = rs_client.put(
        "/RescheduleBooking",
        data={
            "new_date": "2099-02-01",
            "new_start": "10:00",
            "new_end": "11:00",
            "new_label": "MORNING",
            "reason": "   ",
        },
        cookies=_cookies("student-1"),
    )

    assert resp.status_code == 400


def test_reschedule_booking_success_books_new_slot_and_releases_old(rs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client, Status="Approved", RescheduledOnce=False, AdvisorId="advisor-1", Date="2099-01-01", Time="09:00-10:00")
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {
            "2099-01-01": [{"start": "09:00", "end": "10:00", "booked": 1, "isLocked": True, "is_closed": True}],
            "2099-02-01": [{"start": "10:00", "end": "11:00", "booked": 0, "max_booking": 1, "isLocked": False, "is_closed": False}],
        },
    })

    resp = rs_client.put(
        "/RescheduleBooking",
        data={
            "new_date": "2099-02-01",
            "new_start": "10:00",
            "new_end": "11:00",
            "new_label": "MORNING",
            "reason": "ติดธุระ",
        },
        cookies=_cookies("student-1"),
    )

    assert resp.status_code == 200
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Rescheduled"
    assert booking["Date"] == "2099-02-01"
    assert mongo_client["BORC"]["RescheduleHistory"].count_documents({"rescheduledByRole": "Student"}) == 1


# ── Show_BookingData ───────────────────────────────────────────────────────────

@pytest.fixture
def sbd_app(mongo_client, monkeypatch):
    from users.router.Students import Show_BookingData as sbd
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(sbd, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(sbd.router)
    return app, sbd


@pytest.fixture
def sbd_client(sbd_app):
    app, _ = sbd_app
    return TestClient(app)


def test_show_data_returns_only_own_bookings(sbd_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    _insert_booking(mongo_client, UserId="student-1")
    _insert_booking(mongo_client, UserId="student-2")

    resp = sbd_client.get("/ShowData", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "student-1"
    assert len(body["bookings"]) == 1
    assert body["bookings"][0]["UserId"] == "student-1"


def test_booking_stats_counts_own_history_only(sbd_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    mongo_client["BORC"]["RescheduleHistory"].insert_many([
        {"rescheduledById": "student-1", "rescheduledByRole": "Student"},
        {"rescheduledById": "other", "rescheduledByRole": "Student"},
    ])
    mongo_client["BORC"]["CancelBookingHistory"].insert_one(
        {"cancelledById": "student-1", "cancelledByRole": "Student"}
    )

    resp = sbd_client.get("/BookingStats", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert resp.json() == {"stats": {"rescheduled": 1, "cancelled": 1}}


# ── ViewConsultationHours ──────────────────────────────────────────────────────

@pytest.fixture
def vch_app(mongo_client, monkeypatch):
    from users.router.Students import ViewConsultationHours as vch
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(vch, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(vch.router)
    return app, vch


@pytest.fixture
def vch_client(vch_app):
    app, _ = vch_app
    return TestClient(app)


def test_get_advisors_lists_advisors_with_available_dates(vch_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "advisor_name": "อ.ทดสอบ",
        "months": ["2099-01"],
        "dates": {"2099-01-01": [{"start": "09:00", "end": "10:00", "isLocked": False, "is_closed": False}]},
    })

    resp = vch_client.get("/Advisors", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    advisors = resp.json()["advisors"]
    assert len(advisors) == 1
    assert advisors[0]["advisorId"] == "advisor-1"


def test_get_advisor_slots_404_when_advisor_missing(vch_client, mongo_client):
    _seed_user(mongo_client, "student-1")

    resp = vch_client.get("/Slots/no-such-advisor", cookies=_cookies("student-1"))

    assert resp.status_code == 404


def test_get_advisor_slots_returns_status_per_slot(vch_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "advisor_name": "อ.ทดสอบ",
        "dates": {"2099-01-01": [{"start": "09:00", "end": "10:00", "isLocked": False, "is_closed": False, "booked": 0, "max_booking": 1}]},
    })

    resp = vch_client.get("/Slots/advisor-1", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["dates"]["2099-01-01"][0]["status"] == "ว่าง"


# ── QueuehistoryStudent ─────────────────────────────────────────────────────────

@pytest.fixture
def qhs_app(mongo_client, monkeypatch):
    from users.router.Students import QueuehistoryStudent as qhs
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(qhs, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(qhs.router)
    return app, qhs


@pytest.fixture
def qhs_client(qhs_app):
    app, _ = qhs_app
    return TestClient(app)


def test_history_returns_only_own_records(qhs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    mongo_client["BORC"]["RescheduleHistory"].insert_many([
        {"rescheduledById": "student-1", "rescheduledByRole": "Student", "status": "Rescheduled"},
        {"rescheduledById": "student-2", "rescheduledByRole": "Student", "status": "Rescheduled"},
    ])
    mongo_client["BORC"]["QueueManagementHistory"].insert_many([
        {"userId": "student-1", "status": "Completed"},
        {"userId": "student-2", "status": "Completed"},
    ])

    resp = qhs_client.get("/History", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    assert {item["status"] for item in data} == {"Rescheduled", "Completed"}


def test_history_includes_advisor_initiated_events(qhs_client, mongo_client):
    _seed_user(mongo_client, "student-1")
    mongo_client["BORC"]["ApprovedHistory"].insert_one({
        "UserId": "student-1", "AdvisorName": "อาจารย์เอ", "Status": "Approved",
    })
    mongo_client["BORC"]["CancelBookingHistory"].insert_one({
        "cancelledById": "student-1", "cancelledByRole": "Advisor",
        "advisorName": "อาจารย์เอ", "status": "Cancelled", "cancelReason": "ติดธุระ",
    })
    mongo_client["BORC"]["RescheduleHistory"].insert_one({
        "studentId": "student-1", "rescheduledById": "advisor-1",
        "rescheduledByRole": "Advisor", "advisorName": "อาจารย์เอ", "status": "Rescheduled",
    })

    resp = qhs_client.get("/History", cookies=_cookies("student-1"))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {item["status"] for item in data} == {"Approved", "Cancelled", "Rescheduled"}
    assert all(item["UserName"] == "อาจารย์เอ" for item in data)


def test_history_rejects_advisor_role(qhs_client, mongo_client):
    _seed_user(mongo_client, "advisor-1", role="Advisor")

    resp = qhs_client.get("/History", cookies=_cookies("advisor-1"))

    assert resp.status_code == 403

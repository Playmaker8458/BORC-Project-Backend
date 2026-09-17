"""
Endpoint coverage สำหรับ Advisor routers ที่ก่อนหน้านี้ไม่มี test เลย:

- ManageQueueAdvisor.py     (AdvisorQueues, ConfirmQueue, AdvisorCancelQueue, CompleteQueue)
- ManageTimeSlots.py        (TimeSlots, SaveTimeSlots, UpdateTimeSlots, DeleteTimeSlots)
- QueuehistoryAdvisor.py    (All)
- Rechedule_Advisor.py      (BookingInfo, AvailableSlots, RescheduleBooking)
- Show_Consult.py           (TodayQueue, AdvisorStats)
- ConsultationAvailability.py (GetDaySlots, SaveDaySchedule)
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token


def _make_token(user_id: str) -> str:
    return encode_token({"user_id": user_id}, timedelta(days=1))


def _seed_user(mongo_client, user_id, role="Advisor", status="Approved", **extra):
    doc = {
        "userId": user_id,
        "Role": role,
        "Status": status,
        "Prefix": "อ.",
        "Firstname": "ทดสอบ",
        "Lastname": "อาจารย์",
        "imageURL": "",
    }
    doc.update(extra)
    mongo_client["BORC"]["UserProfile"].insert_one(doc)


def _cookies(user_id: str) -> dict:
    return {"access_token": _make_token(user_id)}


def _insert_booking(mongo_client, **kw):
    now = datetime.now(timezone.utc)
    doc = {
        "UserId": "student-1",
        "StudentName": "นาย ทดสอบ นักศึกษา",
        "Advisor_Name": "อ.ทดสอบ อาจารย์",
        "AdvisorId": "advisor-1",
        "Date": "2099-01-01",
        "Time": "09:00-10:00",
        "TimeLabel": "MORNING",
        "ResearchTopic": "Topic",
        "Status": "Pending",
        "RescheduledOnce": False,
        "AdvisorRescheduledOnce": False,
        "CreatedAt": now,
        "UpdatedAt": now,
    }
    doc.update(kw)
    return mongo_client["BORC"]["BookingOnline"].insert_one(doc)


# ── ManageQueueAdvisor ──────────────────────────────────────────────────────────

@pytest.fixture
def mqa_app(mongo_client, monkeypatch):
    from users.router.Advisor import ManageQueueAdvisor as mqa
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(mqa, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(mqa, "auto_update_status", lambda db: None)

    app = FastAPI()
    app.include_router(mqa.router)
    return app, mqa


@pytest.fixture
def mqa_client(mqa_app):
    app, _ = mqa_app
    return TestClient(app)


def test_advisor_queues_lists_own_active_bookings(mqa_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Pending")
    _insert_booking(mongo_client, AdvisorId="other-advisor", Status="Pending")

    resp = mqa_client.get("/AdvisorQueues", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    queues = resp.json()["queues"]
    assert len(queues) == 1
    assert queues[0]["AdvisorId"] == "advisor-1"


def test_confirm_queue_success(mqa_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Pending")

    resp = mqa_client.put("/ConfirmQueue", json={"user_id": "student-1"}, cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Approved"
    assert mongo_client["BORC"]["ApprovedHistory"].count_documents({}) == 1


def test_confirm_queue_cannot_confirm_another_advisors_booking(mqa_client, mongo_client):
    """Cross-tenant check: advisor-2 must not confirm advisor-1's booking."""
    _seed_user(mongo_client, "advisor-1")
    _seed_user(mongo_client, "advisor-2")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Pending")

    resp = mqa_client.put("/ConfirmQueue", json={"user_id": "student-1"}, cookies=_cookies("advisor-2"))

    assert resp.status_code == 404
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Pending"


def test_confirm_queue_rejects_non_confirmable_status(mqa_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved")

    resp = mqa_client.put("/ConfirmQueue", json={"user_id": "student-1"}, cookies=_cookies("advisor-1"))

    assert resp.status_code == 400


def test_advisor_cancel_queue_success(mqa_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Pending", Date="2099-01-01", Time="09:00-10:00")

    resp = mqa_client.request(
        "DELETE", "/AdvisorCancelQueue",
        json={"user_id": "student-1", "reason": "ไม่สะดวก"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Cancelled"


def test_complete_queue_success(mqa_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved")

    resp = mqa_client.patch("/CompleteQueue", json={"user_id": "student-1"}, cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Completed"


def test_complete_queue_404_when_no_matching_booking(mqa_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")

    resp = mqa_client.patch("/CompleteQueue", json={"user_id": "no-such-student"}, cookies=_cookies("advisor-1"))

    assert resp.status_code == 404


# ── ManageTimeSlots ─────────────────────────────────────────────────────────────

@pytest.fixture
def mts_app(mongo_client, monkeypatch):
    from users.router.Advisor import ManageTimeSlots as mts
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(mts, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(mts.router)
    return app, mts


@pytest.fixture
def mts_client(mts_app):
    app, _ = mts_app
    return TestClient(app)


def test_save_time_slots_success(mts_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    future_date = "2099-05-01"

    resp = mts_client.post(
        "/SaveTimeSlots",
        json={"dates": {future_date: [{"start": "09:00", "end": "09:30", "max_booking": 1}]}},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    assert doc is not None
    assert len(doc["dates"][future_date]) == 1


def test_save_time_slots_rejects_overlap(mts_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    future_date = "2099-05-01"

    resp = mts_client.post(
        "/SaveTimeSlots",
        json={"dates": {future_date: [
            {"start": "09:00", "end": "10:00", "max_booking": 1},
            {"start": "09:30", "end": "10:30", "max_booking": 1},
        ]}},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 400


def test_save_time_slots_rejects_past_date(mts_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")

    resp = mts_client.post(
        "/SaveTimeSlots",
        json={"dates": {"2020-01-01": [{"start": "09:00", "end": "09:30"}]}},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 400


def test_save_time_slots_allows_future_date_regardless_of_proximity(mts_app, mts_client, mongo_client):
    """หลังเปลี่ยนนโยบายจาก 'ห้ามแตะวันพรุ่งนี้' -> 'ห้ามแตะเฉพาะนัดหมายที่มีคนจองแล้ว':
    วันพรุ่งนี้ที่ยังไม่มีใครจองต้องสร้าง slot ใหม่ได้ตามปกติ ไม่ถูกบล็อกแค่เพราะใกล้"""
    _, mts = mts_app
    _seed_user(mongo_client, "advisor-1")
    tomorrow = (mts.get_today_utc7() + timedelta(days=1)).isoformat()

    resp = mts_client.post(
        "/SaveTimeSlots",
        json={"dates": {tomorrow: [{"start": "09:00", "end": "09:30"}]}},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    assert len(doc["dates"][tomorrow]) == 1


def test_copy_to_all_days_skips_days_with_existing_booking_regardless_of_proximity(
    mts_app, mts_client, mongo_client
):
    """CopyToAllDays ต้อง skip เฉพาะวันที่มีนักศึกษาจองอยู่แล้วจริง ๆ (ไม่ว่าจะใกล้แค่ไหน)
    ไม่ใช่ skip ตามระยะห่างวันที่แบบเดิม"""
    _, mts = mts_app
    _seed_user(mongo_client, "advisor-1")
    tomorrow = mts.get_today_utc7() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
    target_month = tomorrow.strftime("%Y-%m")

    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {tomorrow_str: [{"start": "08:00", "end": "08:30", "booked": 1, "isLocked": True}]},
        "months": [target_month],
    })

    resp = mts_client.post(
        "/CopyToAllDays",
        json={
            "target_month": target_month,
            "slots": [{"start": "09:00", "end": "09:30"}],
        },
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    # วันพรุ่งนี้มีนัดหมายอยู่แล้ว (booked=1) -> ต้องถูก skip ไม่ถูกเขียนทับ
    assert doc["dates"][tomorrow_str] == [
        {"start": "08:00", "end": "08:30", "booked": 1, "isLocked": True}
    ]
    warning = resp.json().get("warning") or ""
    assert tomorrow_str not in warning


def test_update_time_slot_rejects_time_change_when_booked_regardless_of_date(
    mts_client, mongo_client
):
    """แกนหลักของ 'ห้ามแก้ไขช่วงเวลาของนัดหมายก่อนถึงวันให้คำปรึกษา' — ทดสอบกับวันที่ไกล
    ออกไปมาก (ไม่ใช่แค่พรุ่งนี้) เพื่อยืนยันว่าการป้องกันอิงจาก booking ไม่ใช่ปฏิทิน"""
    _seed_user(mongo_client, "advisor-1")
    far_future_date = "2099-12-25"
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {far_future_date: [{"start": "09:00", "end": "09:30", "booked": 1, "isLocked": False}]},
    })

    resp = mts_client.put(
        f"/UpdateTimeSlots/{far_future_date}",
        json={"old_start": "09:00", "old_end": "09:30", "new_start": "10:00", "new_end": "10:30"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 400
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    assert doc["dates"][far_future_date][0]["start"] == "09:00"


def test_delete_time_slot_rejects_when_booked_regardless_of_date(mts_client, mongo_client):
    """เช่นเดียวกัน สำหรับ Delete — นัดหมายที่ไกลออกไปมากก็ยังลบไม่ได้ถ้ามีคนจองแล้ว"""
    _seed_user(mongo_client, "advisor-1")
    far_future_date = "2099-12-25"
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {far_future_date: [{"start": "09:00", "end": "09:30", "booked": 1, "isLocked": False}]},
    })

    resp = mts_client.request(
        "DELETE", f"/DeleteTimeSlots/{far_future_date}",
        json={"start": "09:00", "end": "09:30"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 400
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    assert len(doc["dates"][far_future_date]) == 1


def test_get_time_slots_requires_advisor_role(mts_client, mongo_client):
    _seed_user(mongo_client, "student-1", role="Student")

    resp = mts_client.get("/TimeSlots", cookies=_cookies("student-1"))

    assert resp.status_code == 403


def test_delete_time_slot_rejects_when_locked(mts_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {"2020-01-01": [{"start": "09:00", "end": "09:30", "isLocked": True, "booked": 1}]},
    })

    resp = mts_client.request(
        "DELETE", "/DeleteTimeSlots/2020-01-01",
        json={"start": "09:00", "end": "09:30"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 400


def test_update_time_slot_success(mts_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {"2020-01-01": [{"start": "09:00", "end": "09:30", "isLocked": False, "booked": 0}]},
    })

    resp = mts_client.put(
        "/UpdateTimeSlots/2020-01-01",
        json={"old_start": "09:00", "old_end": "09:30", "new_start": "10:00", "new_end": "10:30"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    assert doc["dates"]["2020-01-01"][0]["start"] == "10:00"


# ── QueuehistoryAdvisor ──────────────────────────────────────────────────────────

@pytest.fixture
def qha_app(mongo_client, monkeypatch):
    from users.router.Advisor import QueuehistoryAdvisor as qha
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(qha, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(qha.router)
    return app, qha


@pytest.fixture
def qha_client(qha_app):
    app, _ = qha_app
    return TestClient(app)


def test_advisor_history_returns_only_own_records(qha_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    mongo_client["BORC"]["QueueManagementHistory"].insert_many([
        {"userId": "advisor-1", "status": "Approved"},
        {"userId": "advisor-2", "status": "Cancelled"},
    ])

    resp = qha_client.get("/All", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["status"] == "Approved"


# ── Rechedule_Advisor ────────────────────────────────────────────────────────────

@pytest.fixture
def ra_app(mongo_client, monkeypatch):
    from users.router.Advisor import Rechedule_Advisor as ra
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ra, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(ra.router)
    return app, ra


@pytest.fixture
def ra_client(ra_app):
    app, _ = ra_app
    return TestClient(app)


def test_booking_info_forbidden_for_other_advisor(ra_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _seed_user(mongo_client, "advisor-2")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved")

    resp = ra_client.get("/BookingInfo", params={"user_id": "student-1"}, cookies=_cookies("advisor-2"))

    assert resp.status_code == 403


def test_booking_info_success(ra_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved", Date="2099-01-01", Time="09:00-10:00")

    resp = ra_client.get("/BookingInfo", params={"user_id": "student-1"}, cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    assert resp.json()["booking"]["advisorId"] == "advisor-1"


def test_reschedule_booking_rejects_second_advisor_reschedule(ra_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved", AdvisorRescheduledOnce=True)

    resp = ra_client.put(
        "/RescheduleBooking",
        json={"user_id": "student-1", "new_date": "2099-02-01", "new_start": "10:00", "new_end": "11:00", "reason": "x"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 400


def test_reschedule_booking_success(ra_client, mongo_client, monkeypatch):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Approved", AdvisorRescheduledOnce=False, Date="2099-01-01", Time="09:00-10:00")

    class _FakeResp:
        def json(self):
            return {"ok": True}

    from users.router.Advisor import Rechedule_Advisor as ra_module
    monkeypatch.setattr(ra_module.http_req, "post", lambda *a, **kw: _FakeResp())

    resp = ra_client.put(
        "/RescheduleBooking",
        json={"user_id": "student-1", "new_date": "2099-02-01", "new_start": "10:00", "new_end": "11:00", "reason": "ติดธุระ"},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    booking = mongo_client["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Rescheduled"
    assert booking["AdvisorRescheduledOnce"] is True


# ── Show_Consult ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sc_app(mongo_client, monkeypatch):
    from users.router.Advisor import Show_Consult as sc
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(sc, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(sc.router)
    return app, sc


@pytest.fixture
def sc_client(sc_app):
    app, _ = sc_app
    return TestClient(app)


def test_today_queue_returns_only_own_pending_bookings(sc_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    _insert_booking(mongo_client, AdvisorId="advisor-1", Status="Pending")
    _insert_booking(mongo_client, AdvisorId="other-advisor", Status="Pending")

    resp = sc_client.get("/TodayQueue", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1


def test_advisor_stats_counts(sc_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    advisor_name = "อ.ทดสอบ อาจารย์"
    mongo_client["BORC"]["ApprovedHistory"].insert_one({"AdvisorId": "advisor-1", "Status": "Approved"})
    # เลื่อนคิวที่อาจารย์กดเลื่อนเอง และที่นักศึกษากดเลื่อนเอง ต้องถูกนับทั้งคู่
    mongo_client["BORC"]["RescheduleHistory"].insert_many([
        {"rescheduledById": "advisor-1", "rescheduledByRole": "Advisor", "advisorName": advisor_name},
        {"rescheduledById": "student-x", "rescheduledByRole": "Student", "advisorName": advisor_name},
    ])
    # ยกเลิกคิวที่นักศึกษากดยกเลิกเอง ต้องถูกนับด้วย (ไม่ใช่แค่ที่อาจารย์กดยกเลิก)
    mongo_client["BORC"]["CancelBookingHistory"].insert_one(
        {"cancelledById": "student-x", "cancelledByRole": "Student", "advisorName": advisor_name}
    )
    mongo_client["BORC"]["QueueManagementHistory"].insert_one(
        {"userId": "advisor-1", "status": "Completed"}
    )

    resp = sc_client.get("/AdvisorStats", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    stats = resp.json()["stats"]
    assert stats["Approved"] == 1
    assert stats["rescheduled"] == 2
    assert stats["cancelled"] == 1
    assert stats["completed"] == 1


# ── ConsultationAvailability ──────────────────────────────────────────────────────

@pytest.fixture
def ca_app(mongo_client, monkeypatch):
    from users.router.Advisor import ConsultationAvailability as ca
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ca, "Connect_MongoDB", lambda: mongo_client)

    app = FastAPI()
    app.include_router(ca.router)
    return app, ca


@pytest.fixture
def ca_client(ca_app):
    app, _ = ca_app
    return TestClient(app)


def test_get_day_slots_empty_when_no_slots(ca_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")

    resp = ca_client.get("/GetDaySlots/2099-01-01", cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    assert resp.json()["slots"] == []


def test_save_day_schedule_closes_unbooked_slots(ca_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1",
        "dates": {"2099-01-01": [{"start": "09:00", "end": "10:00", "is_closed": False, "booked": 0}]},
    })

    resp = ca_client.put(
        "/SaveDaySchedule",
        json={"date": "2099-01-01", "day_closed": True, "slots": [{"start": "09:00", "end": "10:00", "is_closed": True}]},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 200
    doc = mongo_client["BORC"]["ManageTimeSlots"].find_one({"advisorId": "advisor-1"})
    assert doc["dates"]["2099-01-01"][0]["is_closed"] is True

    av_doc = mongo_client["BORC"]["ConsultationAvailability"].find_one({"advisorId": "advisor-1", "date": "2099-01-01"})
    assert av_doc["day_closed"] is True


def test_save_day_schedule_404_when_no_slots_for_date(ca_client, mongo_client):
    _seed_user(mongo_client, "advisor-1")

    resp = ca_client.put(
        "/SaveDaySchedule",
        json={"date": "2099-01-01", "day_closed": True, "slots": []},
        cookies=_cookies("advisor-1"),
    )

    assert resp.status_code == 404

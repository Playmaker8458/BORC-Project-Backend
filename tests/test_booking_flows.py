"""
Characterization tests สำหรับ flow ที่ซับซ้อนที่สุดของ backend ก่อน/หลัง refactor:

- Students/BookingOnline.py        POST /BookingOnline            (create_booking)
- Students/Reschedule_Students.py  PUT  /RescheduleBooking        (นักศึกษาเลื่อนคิว)
- Advisor/Rechedule_Advisor.py     PUT  /RescheduleBooking        (อาจารย์เลื่อนคิว)

เป้าหมายคือยืนยันว่าการแยกฟังก์ชันย่อยไม่เปลี่ยนพฤติกรรม: status code, ข้อมูลที่เขียนลง DB
(booking / slot / history) และ payload ที่ส่งไปแจ้งเตือน

หมายเหตุ: mongomock ไม่รองรับ array_filters ที่ create_booking ใช้ล็อก slot จึงใช้ proxy
ที่จำลอง semantics เท่าที่จำเป็น (ดู _ArrayFilterCollection)
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token

FUTURE = "2099-01-01"
FUTURE2 = "2099-02-01"


# ── helpers ───────────────────────────────────────────────────────────────────

def _cookies(user_id):
    return {"access_token": encode_token({"user_id": user_id}, timedelta(days=1))}


def _seed_user(mongo_client, user_id, role):
    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": user_id, "Role": role, "Status": "Approved",
        "Prefix": "นาย", "Firstname": "ทดสอบ", "Lastname": "ระบบ", "imageURL": "",
    })


def _slot(start="09:00", end="10:00", **kw):
    s = {"start": start, "end": end, "label": "", "isLocked": False,
         "is_closed": False, "booked": 0, "max_booking": 1}
    s.update(kw)
    return s


def _seed_slots(mongo_client, dates, advisor_id="advisor-1"):
    mongo_client["BORC"]["ManageTimeSlots"].insert_one({
        "advisorId": advisor_id, "advisor_name": "อ.ทดสอบ", "dates": dates,
    })


def _seed_two_days(mongo, new_slot=None):
    _seed_slots(mongo, {
        FUTURE: [_slot(booked=1, isLocked=True, is_closed=True)],
        FUTURE2: [new_slot or _slot("10:00", "11:00")],
    })


def _insert_booking(mongo_client, **kw):
    now = datetime.now(timezone.utc)
    doc = {
        "UserId": "student-1", "Advisor_Name": "อ.ทดสอบ", "AdvisorId": "advisor-1",
        "Date": FUTURE, "Time": "09:00-10:00", "ResearchTopic": "T", "ResearchDetail": "D",
        "Status": "Approved", "RescheduledOnce": False, "AdvisorRescheduledOnce": False,
        "StudentName": "นาย ทดสอบ ระบบ", "CreatedAt": now, "UpdatedAt": now,
    }
    doc.update(kw)
    return mongo_client["BORC"]["BookingOnline"].insert_one(doc)


class _Result:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class _ArrayFilterCollection:
    """proxy รอบ mongomock collection: จำลอง update_one(..., array_filters=[{slot.*}])"""

    def __init__(self, col):
        self._col = col

    def __getattr__(self, name):
        return getattr(self._col, name)

    def update_one(self, query, update, *args, array_filters=None, **kw):
        if not array_filters:
            return self._col.update_one(query, update, *args, **kw)

        flt = array_filters[0]
        doc = self._col.find_one(query)
        if doc is None:
            return _Result(0)

        def matches(slot):
            for key, want in flt.items():
                field = key.split(".", 1)[1]
                if isinstance(want, dict) and "$ne" in want:
                    if slot.get(field) == want["$ne"]:
                        return False
                elif slot.get(field) != want:
                    return False
            return True

        # ตรวจเงื่อนไขก่อนแก้ใด ๆ (เหมือน MongoDB ที่ filter ถูกประเมินกับสถานะก่อน update)
        targets = []
        for path in list(update.get("$inc", {})) + list(update.get("$set", {})):
            _, date, _, _ = path.split(".")
            targets.extend(s for s in doc["dates"][date] if matches(s))
        ids = {id(s) for s in targets}

        modified = 0
        for path, delta in update.get("$inc", {}).items():
            _, date, _, field = path.split(".")
            for slot in doc["dates"][date]:
                if id(slot) in ids:
                    slot[field] = slot.get(field, 0) + delta
                    modified = 1
        for path, value in update.get("$set", {}).items():
            _, date, _, field = path.split(".")
            for slot in doc["dates"][date]:
                if id(slot) in ids:
                    slot[field] = value
                    modified = 1
        if modified:
            self._col.replace_one({"_id": doc["_id"]}, doc)
        return _Result(modified)


class _DB:
    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        col = self._db[name]
        return _ArrayFilterCollection(col) if name == "ManageTimeSlots" else col


class _Client:
    def __init__(self, client):
        self._client = client

    def __getitem__(self, name):
        return _DB(self._client[name])


# ── create_booking ────────────────────────────────────────────────────────────

@pytest.fixture
def bo_ctx(mongo_client, monkeypatch):
    from users.router.Students import BookingOnline as bo
    from users.auth import authUser

    client = _Client(mongo_client)
    notified = []
    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(bo, "Connect_MongoDB", lambda: client)
    monkeypatch.setattr(bo, "auto_update_status", lambda db: None)
    monkeypatch.setattr(bo, "_notify_advisor_background", lambda url, payload: notified.append((url, payload)))

    app = FastAPI()
    app.include_router(bo.router)
    _seed_user(mongo_client, "student-1", "Student")
    return TestClient(app), mongo_client, notified


def _form(**kw):
    d = {"advisor_id": "advisor-1", "advisor_name": "ignored-by-server", "date": FUTURE,
         "time": "09:00-10:00", "research_topic": "หัวข้อ", "research_detail": "รายละเอียด"}
    d.update(kw)
    return d


def test_create_booking_success_locks_slot_inserts_booking_and_notifies(bo_ctx):
    client, mongo, notified = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot()]})

    resp = client.post("/BookingOnline", data=_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 200
    booking = mongo["BORC"]["BookingOnline"].find_one({"UserId": "student-1"})
    assert booking["Status"] == "Pending"
    assert booking["AdvisorId"] == "advisor-1"
    assert booking["Advisor_Name"] == "อ.ทดสอบ"  # ชื่อจากตาราง slot ไม่ใช่จาก form
    assert booking["StudentName"] == "นายทดสอบ ระบบ"
    assert booking["Time"] == "09:00-10:00"
    assert booking["RescheduledOnce"] is False
    slot = mongo["BORC"]["ManageTimeSlots"].find_one({})["dates"][FUTURE][0]
    assert (slot["booked"], slot["isLocked"], slot["is_closed"]) == (1, True, True)
    assert len(notified) == 1
    assert notified[0][0].endswith("/NotifyQueueAdivsor/BookingStudent")
    assert notified[0][1] == {
        "AdvisorId": "advisor-1", "StudentName": "นายทดสอบ ระบบ", "ResearchTopic": "หัวข้อ",
        "Date": FUTURE, "Time": "09:00-10:00", "Status": "Pending",
    }


def test_create_booking_requires_auth(bo_ctx):
    client, mongo, _ = bo_ctx
    assert client.post("/BookingOnline", data=_form()).status_code == 401


@pytest.mark.parametrize("status", ["Pending", "Approved", "InProgress", "Rescheduled"])
def test_create_booking_blocked_by_active_booking(bo_ctx, status):
    client, mongo, notified = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot()]})
    _insert_booking(mongo, Status=status)

    resp = client.post("/BookingOnline", data=_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 400
    assert "ยังไม่เสร็จสิ้น" in resp.json()["detail"]
    assert notified == []
    assert mongo["BORC"]["ManageTimeSlots"].find_one({})["dates"][FUTURE][0]["booked"] == 0


@pytest.mark.parametrize("status", ["Cancelled", "Completed"])
def test_create_booking_after_finished_booking_inserts_new_document(bo_ctx, status):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot()]})
    _insert_booking(mongo, Status=status, Date="2098-01-01")

    resp = client.post("/BookingOnline", data=_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 200
    assert mongo["BORC"]["BookingOnline"].count_documents({"UserId": "student-1"}) == 2


def test_create_booking_rejects_past_date(bo_ctx):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {"2000-01-01": [_slot()]})

    resp = client.post("/BookingOnline", data=_form(date="2000-01-01"), cookies=_cookies("student-1"))

    assert resp.status_code == 400


def test_create_booking_rejects_within_one_hour_cutoff(bo_ctx):
    client, mongo, _ = bo_ctx
    today = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d")
    _seed_slots(mongo, {today: [_slot("00:00", "00:01")]})

    resp = client.post("/BookingOnline", data=_form(date=today, time="00:00-00:01"), cookies=_cookies("student-1"))

    assert resp.status_code == 400
    assert "ล่วงหน้า" in resp.json()["detail"]


def test_create_booking_404_when_no_slot_doc_for_date(bo_ctx):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {FUTURE2: [_slot()]})

    resp = client.post("/BookingOnline", data=_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 404


def test_create_booking_404_when_time_not_in_slots(bo_ctx):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot("11:00", "12:00")]})

    resp = client.post("/BookingOnline", data=_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 404


@pytest.mark.parametrize("slot_kw", [{"isLocked": True}, {"is_closed": True}, {"booked": 1}])
def test_create_booking_rejects_unavailable_slot(bo_ctx, slot_kw):
    client, mongo, notified = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot(**slot_kw)]})

    resp = client.post("/BookingOnline", data=_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 400
    assert mongo["BORC"]["BookingOnline"].count_documents({}) == 0
    assert notified == []


def test_create_booking_rejects_bad_file_type(bo_ctx):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot()]})

    resp = client.post(
        "/BookingOnline", data=_form(), cookies=_cookies("student-1"),
        files={"file": ("a.exe", b"x", "application/octet-stream")},
    )

    assert resp.status_code == 400


def test_create_booking_rejects_oversize_file(bo_ctx):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot()]})

    resp = client.post(
        "/BookingOnline", data=_form(), cookies=_cookies("student-1"),
        files={"file": ("a.pdf", b"x" * (10 * 1024 * 1024 + 1), "application/pdf")},
    )

    assert resp.status_code == 400
    assert mongo["BORC"]["BookingOnline"].count_documents({}) == 0


def test_create_booking_accepts_pdf_and_records_filename(bo_ctx):
    client, mongo, _ = bo_ctx
    _seed_slots(mongo, {FUTURE: [_slot()]})

    resp = client.post(
        "/BookingOnline", data=_form(), cookies=_cookies("student-1"),
        files={"file": ("plan.pdf", b"%PDF", "application/pdf")},
    )

    assert resp.status_code == 200
    assert mongo["BORC"]["BookingOnline"].find_one({})["FilePath"] == "plan.pdf"


# ── student reschedule ────────────────────────────────────────────────────────

@pytest.fixture
def rs_ctx(mongo_client, monkeypatch):
    from users.router.Students import Reschedule_Students as rs
    from users.auth import authUser

    notified = []
    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(rs, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(rs, "notify_chatbot", lambda url, payload, headers: notified.append((url, payload)))

    app = FastAPI()
    app.include_router(rs.router)
    _seed_user(mongo_client, "student-1", "Student")
    return TestClient(app), mongo_client, notified


def _rs_form(**kw):
    d = {"new_date": FUTURE2, "new_start": "10:00", "new_end": "11:00",
         "new_label": "MORNING", "reason": "ติดธุระ"}
    d.update(kw)
    return d


def test_student_reschedule_404_without_active_booking(rs_ctx):
    client, mongo, _ = rs_ctx
    assert client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1")).status_code == 404


@pytest.mark.parametrize("status", ["Pending", "Rescheduled", "InProgress"])
def test_student_reschedule_requires_approved_status(rs_ctx, status):
    client, mongo, _ = rs_ctx
    _insert_booking(mongo, Status=status)
    assert client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1")).status_code == 400


def test_student_reschedule_only_once(rs_ctx):
    client, mongo, _ = rs_ctx
    _insert_booking(mongo, RescheduledOnce=True)
    assert client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1")).status_code == 400


def test_student_reschedule_blocked_inside_cutoff_window(rs_ctx):
    client, mongo, _ = rs_ctx
    now7 = datetime.now(timezone.utc) + timedelta(hours=7)
    _insert_booking(mongo, Date=now7.strftime("%Y-%m-%d"), Time=now7.strftime("%H:%M") + "-23:59")
    resp = client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1"))
    assert resp.status_code == 400
    assert "1 ชั่วโมง" in resp.json()["detail"]


@pytest.mark.parametrize("slot_kw", [{"isLocked": True}, {"is_closed": True}, {"booked": 1}])
def test_student_reschedule_rejects_unavailable_new_slot(rs_ctx, slot_kw):
    client, mongo, notified = rs_ctx
    _insert_booking(mongo)
    _seed_two_days(mongo, _slot("10:00", "11:00", **slot_kw))

    resp = client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 400
    assert mongo["BORC"]["BookingOnline"].find_one({})["Status"] == "Approved"
    assert notified == []


def test_student_reschedule_404_for_unknown_new_slot(rs_ctx):
    client, mongo, _ = rs_ctx
    _insert_booking(mongo)
    _seed_two_days(mongo, _slot("13:00", "14:00"))
    assert client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1")).status_code == 404


def test_student_reschedule_success_full_side_effects(rs_ctx):
    client, mongo, notified = rs_ctx
    _insert_booking(mongo)
    _seed_two_days(mongo)

    resp = client.put("/RescheduleBooking", data=_rs_form(), cookies=_cookies("student-1"))

    assert resp.status_code == 200
    b = mongo["BORC"]["BookingOnline"].find_one({})
    assert (b["Status"], b["Date"], b["Time"], b["RescheduledOnce"]) == ("Rescheduled", FUTURE2, "10:00-11:00", True)
    dates = mongo["BORC"]["ManageTimeSlots"].find_one({})["dates"]
    old, new = dates[FUTURE][0], dates[FUTURE2][0]
    assert (old["booked"], old["isLocked"], old["is_closed"]) == (0, False, False)
    assert (new["booked"], new["isLocked"], new["is_closed"]) == (1, True, True)
    h = mongo["BORC"]["RescheduleHistory"].find_one({})
    assert (h["rescheduledById"], h["rescheduledByRole"], h["oldDate"], h["oldStart"], h["oldEnd"]) == \
        ("student-1", "Student", FUTURE, "09:00", "10:00")
    assert (h["newDate"], h["newStart"], h["newEnd"], h["rescheduledReason"]) == (FUTURE2, "10:00", "11:00", "ติดธุระ")
    assert mongo["BORC"]["QueueManagementHistory"].count_documents({"status": "Rescheduled"}) >= 1
    assert len(notified) == 1
    assert notified[0][0].endswith("/NotifyQueueAdivsor/RecheduleAdvisor")
    assert notified[0][1] == {"AdvisorId": "advisor-1", "StudentName": "นาย ทดสอบ ระบบ", "Date": FUTURE2,
                              "Time": "10:00-11:00", "Status": "Rescheduled"}


# ── advisor reschedule ────────────────────────────────────────────────────────

@pytest.fixture
def ra_ctx(mongo_client, monkeypatch):
    from users.router.Advisor import Rechedule_Advisor as ra
    from users.auth import authUser

    notified = []
    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ra, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(ra, "notify_chatbot", lambda url, payload, headers: notified.append((url, payload)))

    app = FastAPI()
    app.include_router(ra.router)
    _seed_user(mongo_client, "advisor-1", "Advisor")
    _seed_user(mongo_client, "advisor-2", "Advisor")
    return TestClient(app), mongo_client, notified


def _ra_body(**kw):
    d = {"user_id": "student-1", "new_date": FUTURE2, "new_start": "10:00", "new_end": "11:00",
         "reason": "ติดธุระ"}
    d.update(kw)
    return d


def test_advisor_reschedule_404_without_booking(ra_ctx):
    client, mongo, _ = ra_ctx
    assert client.put("/RescheduleBooking", json=_ra_body(), cookies=_cookies("advisor-1")).status_code == 404


def test_advisor_reschedule_403_for_other_advisors_booking(ra_ctx):
    client, mongo, _ = ra_ctx
    _insert_booking(mongo)
    assert client.put("/RescheduleBooking", json=_ra_body(), cookies=_cookies("advisor-2")).status_code == 403


@pytest.mark.parametrize("status", ["Pending", "Rescheduled", "InProgress"])
def test_advisor_reschedule_requires_approved(ra_ctx, status):
    client, mongo, _ = ra_ctx
    _insert_booking(mongo, Status=status)
    assert client.put("/RescheduleBooking", json=_ra_body(), cookies=_cookies("advisor-1")).status_code == 400


def test_advisor_reschedule_blocked_inside_cutoff_window(ra_ctx):
    client, mongo, _ = ra_ctx
    now7 = datetime.now(timezone.utc) + timedelta(hours=7)
    _insert_booking(mongo, Date=now7.strftime("%Y-%m-%d"), Time=now7.strftime("%H:%M") + "-23:59")
    assert client.put("/RescheduleBooking", json=_ra_body(), cookies=_cookies("advisor-1")).status_code == 400


@pytest.mark.parametrize("slot_kw", [{"isLocked": True}, {"is_closed": True}, {"booked": 1}])
def test_advisor_reschedule_rejects_unavailable_new_slot(ra_ctx, slot_kw):
    client, mongo, notified = ra_ctx
    _insert_booking(mongo)
    _seed_two_days(mongo, _slot("10:00", "11:00", **slot_kw))

    resp = client.put("/RescheduleBooking", json=_ra_body(), cookies=_cookies("advisor-1"))

    assert resp.status_code == 400
    assert mongo["BORC"]["BookingOnline"].find_one({})["Status"] == "Approved"
    assert notified == []


def test_advisor_reschedule_success_full_side_effects(ra_ctx):
    client, mongo, notified = ra_ctx
    _insert_booking(mongo)
    _seed_two_days(mongo)

    resp = client.put("/RescheduleBooking", json=_ra_body(), cookies=_cookies("advisor-1"))

    assert resp.status_code == 200
    b = mongo["BORC"]["BookingOnline"].find_one({})
    assert (b["Status"], b["Date"], b["Time"], b["AdvisorRescheduledOnce"], b["RescheduledOnce"]) == \
        ("Rescheduled", FUTURE2, "10:00-11:00", True, False)
    dates = mongo["BORC"]["ManageTimeSlots"].find_one({})["dates"]
    assert dates[FUTURE][0]["booked"] == 0 and dates[FUTURE][0]["isLocked"] is False
    assert dates[FUTURE2][0]["booked"] == 1 and dates[FUTURE2][0]["isLocked"] is True
    h = mongo["BORC"]["RescheduleHistory"].find_one({})
    assert (h["rescheduledById"], h["rescheduledByRole"], h["studentId"]) == ("advisor-1", "Advisor", "student-1")
    assert len(notified) == 1
    assert notified[0][0].endswith("/NotifyQueueStudent/RecheduleStudent")
    assert notified[0][1] == {"UserId": "student-1", "StudentName": "นาย ทดสอบ ระบบ", "Date": FUTURE2,
                              "Time": "10:00-11:00", "Status": "Rescheduled"}

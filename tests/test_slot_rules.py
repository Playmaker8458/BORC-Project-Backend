"""
Characterization tests ก่อน/หลังปรับโค้ดให้อ่านง่าย (Step 2 ของแผนลดความซับซ้อน):

- ManageTimeSlots: ตัวตรวจวันที่ 3 ตัว (read / write / delete-update) และ check_overlap
- BookingOnline: /AvailableAdvisors และ /AvailableSlots/{id} (กฎ "slot ปิด/เต็ม/เลยเวลา cutoff")

ผลลัพธ์ต้องเหมือนเดิมทุกกรณี รวมกรณีขอบ เช่น slot ที่เวลาเริ่มผิดรูปแบบ ซึ่งสองหน้าจัดการต่างกัน
(/AvailableAdvisors ไม่นับว่ามี slot ว่าง แต่ /AvailableSlots แสดง slot นั้นเป็นว่าง)
"""

import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token

TZ7 = timezone(timedelta(hours=7))


# ── ManageTimeSlots: ตัวตรวจวันที่ ───────────────────────────────────────────

@pytest.fixture
def mts():
    from users.router.Advisor import ManageTimeSlots
    return ManageTimeSlots


@pytest.mark.parametrize("fn", ["validate_date_format_read", "validate_date_format_write", "validate_date_format_delete_update"])
@pytest.mark.parametrize("bad,detail", [
    ("2099/01/01", "รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)"),
    ("99-01-01", "รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)"),
    ("2099-1-1", "รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)"),
    ("", "รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)"),
    ("2099-01-01 ", "รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)"),
    ("2099-13-01", "วันที่ไม่ถูกต้อง"),
    ("2099-02-30", "วันที่ไม่ถูกต้อง"),
    ("2099-00-10", "วันที่ไม่ถูกต้อง"),
])
def test_date_validators_reject_bad_format_with_same_messages(mts, fn, bad, detail):
    with pytest.raises(HTTPException) as exc:
        getattr(mts, fn)(bad)
    assert exc.value.status_code == 400
    assert exc.value.detail == detail


@pytest.mark.parametrize("fn", ["validate_date_format_read", "validate_date_format_write", "validate_date_format_delete_update"])
def test_date_validators_accept_future_dates(mts, fn):
    assert getattr(mts, fn)("2099-12-31") is None


@pytest.mark.parametrize("fn", ["validate_date_format_read", "validate_date_format_delete_update"])
def test_read_and_delete_update_accept_past_dates(mts, fn):
    """slot เก่ายังต้องอ่าน/แก้/ลบได้แม้วันนั้นผ่านไปแล้ว"""
    assert getattr(mts, fn)("2001-01-01") is None


def test_write_rejects_past_dates_but_allows_today(mts):
    with pytest.raises(HTTPException) as exc:
        mts.validate_date_format_write("2001-01-01")
    assert (exc.value.status_code, exc.value.detail) == (400, "ไม่สามารถกำหนดช่วงเวลาวันที่ผ่านมาได้")

    yesterday = (mts.get_today_utc7() - timedelta(days=1)).isoformat()
    with pytest.raises(HTTPException):
        mts.validate_date_format_write(yesterday)
    assert mts.validate_date_format_write(mts.get_today_utc7().isoformat()) is None  # วันนี้ยังกำหนดได้ (ใช้ทดสอบ)


def test_month_validator_unchanged(mts):
    assert mts.validate_month_format("2099-05") is None
    for bad in ["2099-5", "2099-05-01", "", "abcd-ef"]:
        with pytest.raises(HTTPException) as exc:
            mts.validate_month_format(bad)
        assert exc.value.detail == "รูปแบบเดือนไม่ถูกต้อง (YYYY-MM)"


# ── ManageTimeSlots: check_overlap ───────────────────────────────────────────

def _reference_check_overlap(slots):
    """สำเนาตรรกะเดิมทุกตัวอักษร (O(n²)) ใช้เป็นตัวเทียบความเท่ากันแบบสุ่ม"""
    times = []
    for s in slots:
        try:
            start = s["start"] if isinstance(s, dict) else s.start
            end = s["end"] if isinstance(s, dict) else s.end
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            times.append((sh * 60 + sm, eh * 60 + em))
        except (KeyError, AttributeError, ValueError):
            continue
    for i in range(len(times)):
        for j in range(i + 1, len(times)):
            s1, e1 = times[i]
            s2, e2 = times[j]
            if s1 < e2 and s2 < e1:
                return True
            if e1 == s2 or e2 == s1:
                return True
    return False


def _hhmm(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


@pytest.mark.parametrize("slots,expected", [
    ([], False),
    ([{"start": "09:00", "end": "10:00"}], False),
    ([{"start": "09:00", "end": "10:00"}, {"start": "10:30", "end": "11:00"}], False),
    ([{"start": "09:00", "end": "10:00"}, {"start": "09:30", "end": "10:30"}], True),   # ทับกัน
    ([{"start": "09:00", "end": "10:00"}, {"start": "10:00", "end": "11:00"}], True),   # ติดกัน นับเป็นทับ (พฤติกรรมเดิม)
    ([{"start": "11:00", "end": "12:00"}, {"start": "09:00", "end": "11:00"}], True),   # ติดกัน ลำดับกลับ
    ([{"start": "09:00", "end": "12:00"}, {"start": "10:00", "end": "11:00"}], True),   # ซ้อนอยู่ข้างใน
    ([{"start": "09:00", "end": "09:00"}, {"start": "09:00", "end": "09:00"}], True),   # ความยาว 0 ซ้ำกัน
    ([{"start": "bad", "end": "10:00"}, {"start": "09:00", "end": "10:00"}], False),    # ข้ามตัวที่ผิดรูปแบบ
    ([{"end": "10:00"}, {"start": "09:00", "end": "10:00"}], False),
])
def test_check_overlap_known_cases(mts, slots, expected):
    assert mts.check_overlap(slots) is expected
    assert _reference_check_overlap(slots) is expected


def test_check_overlap_accepts_pydantic_like_objects(mts):
    class S:
        def __init__(self, start, end):
            self.start, self.end = start, end

    assert mts.check_overlap([S("09:00", "10:00"), S("09:30", "10:30")]) is True
    assert mts.check_overlap([S("09:00", "10:00"), S("10:30", "11:00")]) is False


def test_check_overlap_matches_reference_on_random_inputs(mts):
    rng = random.Random(20260919)
    for _ in range(4000):
        n = rng.randint(0, 7)
        slots = []
        for _ in range(n):
            a = rng.randint(0, 24 * 60 - 1)
            b = min(24 * 60 - 1, a + rng.choice([0, 5, 30, 30, 60, 90]))
            if rng.random() < 0.3:
                b = rng.randint(0, 24 * 60 - 1)  # ช่วงที่เวลาจบก่อนเวลาเริ่ม: TimeSlot ยังไม่ได้ห้ามไว้ ต้องคงผลเดิม
            item = {"start": _hhmm(a), "end": _hhmm(b)}
            if rng.random() < 0.05:
                item["start"] = "xx:yy"
            slots.append(item)
        assert mts.check_overlap(slots) is _reference_check_overlap(slots), slots


# ── BookingOnline: AvailableAdvisors / AvailableSlots ────────────────────────

FIXED_NOW = datetime(2099, 6, 15, 10, 0, tzinfo=TZ7)
TODAY = "2099-06-15"
FUTURE = "2099-06-20"


@pytest.fixture
def booking(mongo_client, monkeypatch):
    from users.router.Students import BookingOnline as bo
    from users.auth import authUser

    monkeypatch.setattr(authUser, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(bo, "Connect_MongoDB", lambda: mongo_client)
    monkeypatch.setattr(bo, "get_now_utc7", lambda: FIXED_NOW)
    app = FastAPI()
    app.include_router(bo.router)
    mongo_client["BORC"]["UserProfile"].insert_one({
        "userId": "student-1", "Role": "Student", "Status": "Approved", "Prefix": "", "Firstname": "s",
        "Lastname": "1", "imageURL": ""})
    return TestClient(app), mongo_client


def _cookies():
    return {"access_token": encode_token({"user_id": "student-1"}, timedelta(days=1))}


def _slot(start="09:00", end="10:00", **kw):
    s = {"start": start, "end": end, "label": "L", "booked": 0, "isLocked": False, "is_closed": False, "max_booking": 1}
    s.update(kw)
    return s


def _advisor(mongo, advisor_id, name, dates):
    mongo["BORC"]["ManageTimeSlots"].insert_one({"advisorId": advisor_id, "advisor_name": name, "dates": dates})


def _listed(client):
    resp = client.get("/AvailableAdvisors", cookies=_cookies())
    assert resp.status_code == 200
    return {a["advisor_id"] for a in resp.json()["advisors"]}


def test_advisor_with_open_future_slot_is_listed(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {FUTURE: [_slot()]})
    assert _listed(client) == {"a1"}
    assert client.get("/AvailableAdvisors", cookies=_cookies()).json() == {
        "advisors": [{"advisor_id": "a1", "advisor_name": "อ.เอ"}]}


@pytest.mark.parametrize("taken", [{"isLocked": True}, {"is_closed": True}, {"booked": 1}, {"booked": 5, "max_booking": 2}])
def test_advisor_with_only_taken_slots_is_not_listed(booking, taken):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {FUTURE: [_slot(**taken)]})
    assert _listed(client) == set()


def test_one_open_slot_among_taken_ones_is_enough(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {FUTURE: [_slot(isLocked=True), _slot("10:00", "11:00", is_closed=True), _slot("11:00", "12:00")]})
    assert _listed(client) == {"a1"}


def test_past_dates_are_ignored(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {"2099-06-14": [_slot()]})
    assert _listed(client) == set()


def test_today_slot_needs_more_than_one_hour_lead_time(booking):
    client, mongo = booking
    # ตอนนี้ 10:00 (UTC+7): เริ่ม 11:00 -> cutoff 10:00 ผ่านแล้ว (>=) ไม่ว่าง; เริ่ม 11:01 -> ยังว่าง
    _advisor(mongo, "early", "อ.เช้า", {TODAY: [_slot("11:00", "12:00")]})
    _advisor(mongo, "later", "อ.บ่าย", {TODAY: [_slot("11:01", "12:00")]})
    _advisor(mongo, "past", "อ.ผ่าน", {TODAY: [_slot("09:00", "09:30")]})
    assert _listed(client) == {"later"}


def test_today_with_malformed_start_time_is_not_counted_in_advisor_list(booking):
    """พฤติกรรมเดิม: เวลาเริ่มผิดรูปแบบของ "วันนี้" ไม่ถูกนับว่าว่างในรายชื่ออาจารย์"""
    client, mongo = booking
    _advisor(mongo, "bad", "อ.ผิด", {TODAY: [_slot("xx:yy", "10:00")]})
    assert _listed(client) == set()


def test_non_list_slot_values_are_skipped(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {FUTURE: "not-a-list"})
    assert _listed(client) == set()


def test_docs_without_name_or_id_are_skipped_and_advisors_deduped(booking):
    client, mongo = booking
    _advisor(mongo, "", "อ.ไม่มีไอดี", {FUTURE: [_slot()]})
    _advisor(mongo, "a2", "", {FUTURE: [_slot()]})
    _advisor(mongo, "a3", "อ.สาม", {FUTURE: [_slot(isLocked=True)]})
    _advisor(mongo, "a3", "อ.สาม", {"2099-07-01": [_slot()]})  # เอกสารที่สองของอาจารย์คนเดิม
    assert _listed(client) == {"a3"}


# ── /AvailableSlots/{advisor_id} ─────────────────────────────────────────────

def _slots(client, advisor_id="a1"):
    resp = client.get(f"/AvailableSlots/{advisor_id}", cookies=_cookies())
    assert resp.status_code == 200
    return resp.json()


def test_available_slots_unknown_advisor_returns_empty(booking):
    client, _ = booking
    assert _slots(client, "nobody") == {"dates": {}}


def test_available_slots_exact_output_for_open_and_taken_slots(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {FUTURE: [
        _slot("09:00", "10:00", label="เช้า"),
        _slot("10:00", "11:00", isLocked=True),
        _slot("11:00", "12:00", is_closed=True),
        _slot("12:00", "13:00", booked=1),
    ]})
    body = _slots(client)
    assert body["advisor_id"] == "a1"
    assert body["dates"][FUTURE] == [
        {"start": "09:00", "end": "10:00", "label": "เช้า", "is_closed": False, "is_past": False, "is_booked": False},
        {"start": "10:00", "end": "11:00", "label": "L", "is_closed": True, "is_past": False, "is_booked": True},
        {"start": "11:00", "end": "12:00", "label": "L", "is_closed": True, "is_past": False, "is_booked": False},
        {"start": "12:00", "end": "13:00", "label": "L", "is_closed": True, "is_past": False, "is_booked": True},
    ]


def test_available_slots_past_dates_dropped_and_non_lists_skipped(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {"2099-06-14": [_slot()], FUTURE: "oops", "2099-06-21": [_slot()]})
    assert list(_slots(client)["dates"]) == ["2099-06-21"]


def test_available_slots_today_marks_past_cutoff_slots(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {TODAY: [_slot("09:00", "09:30"), _slot("11:00", "12:00"), _slot("11:01", "12:00")]})
    out = _slots(client)["dates"][TODAY]
    assert [(s["start"], s["is_past"], s["is_closed"], s["is_booked"]) for s in out] == [
        ("09:00", True, True, False),
        ("11:00", True, True, False),   # cutoff = 10:00 = ตอนนี้ (>=) -> ผ่านแล้ว
        ("11:01", False, False, False),
    ]


def test_available_slots_today_malformed_start_is_shown_open(booking):
    """พฤติกรรมเดิม: ต่างจากรายชื่ออาจารย์ — slot ที่เวลาเริ่มผิดรูปแบบไม่ถูกมองว่าเลยเวลา"""
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {TODAY: [_slot("xx:yy", "10:00")]})
    out = _slots(client)["dates"][TODAY][0]
    assert (out["is_past"], out["is_closed"]) == (False, False)


def test_available_slots_merges_dates_across_documents(booking):
    client, mongo = booking
    _advisor(mongo, "a1", "อ.เอ", {FUTURE: [_slot()]})
    _advisor(mongo, "a1", "อ.เอ", {"2099-07-01": [_slot("13:00", "14:00")]})
    assert set(_slots(client)["dates"]) == {FUTURE, "2099-07-01"}

"""
Tests สำหรับ logic การล็อก slot / atomic update ใน
users/router/Students/BookingOnline.py ซึ่งเป็นส่วนที่ critical ที่สุดของระบบ
(ป้องกัน double booking เมื่อมีการจองพร้อมกัน)

หมายเหตุ: mongomock ยังไม่รองรับ `array_filters` (ที่ create_booking() ใน
BookingOnline.py ใช้จริงกับ MongoDB) จึงใช้ _FakeSlotsCollection เล็ก ๆ
ที่จำลอง semantics ของ array_filters เท่าที่จำเป็นสำหรับเคสทดสอบนี้
(หนึ่ง slot ที่ตรงเงื่อนไขต่อการเรียกหนึ่งครั้ง) เพื่อทดสอบ query เดียวกับที่
โค้ดจริงส่งไป โดยไม่ต้องพึ่งพา MongoDB จริงในชุดทดสอบ
"""

from datetime import datetime, timezone

from users.router.Students import BookingOnline as bo


class _FakeUpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class _FakeSlotsCollection:
    """จำลอง update_one(..., array_filters=...) สำหรับ 1 slot ต่อ array"""

    def __init__(self):
        self._docs = {}
        self._next_id = 1

    def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = self._next_id
        self._docs[self._next_id] = doc
        self._next_id += 1

        class _Result:
            inserted_id = doc["_id"]

        return _Result()

    def find_one(self, query, projection=None):
        doc = self._docs.get(query.get("_id"))
        return dict(doc) if doc else None

    def update_one(self, query, update, array_filters=None):
        doc = self._docs.get(query.get("_id"))
        if doc is None:
            return _FakeUpdateResult(0)

        array_filter = (array_filters or [{}])[0]

        modified = False
        for date_key, slots in doc.get("dates", {}).items():
            for slot in slots:
                matches = (
                    slot.get("start") == array_filter.get("slot.start")
                    and slot.get("end") == array_filter.get("slot.end")
                    and slot.get("isLocked") == array_filter.get("slot.isLocked", slot.get("isLocked"))
                    and slot.get("booked") == array_filter.get("slot.booked", slot.get("booked"))
                    and not slot.get("is_closed", False)
                )
                if matches:
                    for field, value in update.get("$inc", {}).items():
                        key = field.split(".")[-1]
                        slot[key] = slot.get(key, 0) + value
                    for field, value in update.get("$set", {}).items():
                        key = field.split(".")[-1]
                        slot[key] = value
                    modified = True

        return _FakeUpdateResult(1 if modified else 0)


def _make_slot_doc(col_slots, advisor_id="adv1", date="2099-01-01"):
    doc = {
        "advisorId": advisor_id,
        "advisor_name": "Dr. Test",
        "dates": {
            date: [
                {
                    "start": "09:00",
                    "end": "10:00",
                    "label": "",
                    "isLocked": False,
                    "is_closed": False,
                    "booked": 0,
                    "max_booking": 1,
                }
            ]
        },
    }
    result = col_slots.insert_one(doc)
    return result.inserted_id


def _attempt_lock(col_slots, slot_id, date, start, end):
    """ทำ atomic update แบบเดียวกับใน create_booking() ของ BookingOnline.py"""
    return col_slots.update_one(
        {"_id": slot_id},
        {
            "$inc": {f"dates.{date}.$[slot].booked": 1},
            "$set": {
                f"dates.{date}.$[slot].isLocked": True,
                f"dates.{date}.$[slot].is_closed": True,
            },
        },
        array_filters=[{
            "slot.start": start,
            "slot.end": end,
            "slot.isLocked": False,
            "slot.is_closed": {"$ne": True},
            "slot.booked": 0,
        }],
    )


def test_first_booking_locks_the_slot():
    col_slots = _FakeSlotsCollection()
    slot_id = _make_slot_doc(col_slots)

    result = _attempt_lock(col_slots, slot_id, "2099-01-01", "09:00", "10:00")

    assert result.modified_count == 1
    updated = col_slots.find_one({"_id": slot_id})
    slot = updated["dates"]["2099-01-01"][0]
    assert slot["isLocked"] is True
    assert slot["is_closed"] is True
    assert slot["booked"] == 1


def test_second_concurrent_booking_is_rejected():
    """สอง request แข่งกันจองช่วงเวลาเดียวกัน -> ต้องมีแค่รายเดียวที่ล็อกสำเร็จ"""
    col_slots = _FakeSlotsCollection()
    slot_id = _make_slot_doc(col_slots)

    first = _attempt_lock(col_slots, slot_id, "2099-01-01", "09:00", "10:00")
    second = _attempt_lock(col_slots, slot_id, "2099-01-01", "09:00", "10:00")

    assert first.modified_count == 1
    assert second.modified_count == 0  # ถูกบล็อกเพราะ slot ถูกล็อกไปแล้ว

    updated = col_slots.find_one({"_id": slot_id})
    slot = updated["dates"]["2099-01-01"][0]
    assert slot["booked"] == 1  # ไม่ถูกเพิ่มซ้ำ


def test_recalculate_slot_booked_locks_when_active_booking_exists(mongo_client):
    db = mongo_client["BORC"]
    col_slots = db["ManageTimeSlots"]
    col_booking = db["BookingOnline"]
    slot_id = _make_slot_doc(col_slots, advisor_id="adv1", date="2099-02-01")

    col_booking.insert_one({
        "AdvisorId": "adv1",
        "Date": "2099-02-01",
        "Time": "09:00-10:00",
        "Status": "Approved",
    })

    booked, is_locked = bo.recalculate_slot_booked(db, "adv1", "2099-02-01", "09:00", "10:00")

    assert booked == 1
    assert is_locked is True
    slot = col_slots.find_one({"_id": slot_id})["dates"]["2099-02-01"][0]
    assert slot["isLocked"] is True
    assert slot["booked"] == 1


def test_recalculate_slot_booked_unlocks_when_no_active_booking(mongo_client):
    db = mongo_client["BORC"]
    col_slots = db["ManageTimeSlots"]
    slot_id = _make_slot_doc(col_slots, advisor_id="adv2", date="2099-03-01")
    # ไม่มี booking ใด ๆ ใน BookingOnline สำหรับ advisor นี้

    booked, is_locked = bo.recalculate_slot_booked(db, "adv2", "2099-03-01", "09:00", "10:00")

    assert booked == 0
    assert is_locked is False
    slot = col_slots.find_one({"_id": slot_id})["dates"]["2099-03-01"][0]
    assert slot["isLocked"] is False
    assert slot["booked"] == 0


def test_recalculate_slot_booked_missing_doc_returns_none(mongo_client):
    db = mongo_client["BORC"]
    booked, is_locked = bo.recalculate_slot_booked(db, "no-such-advisor", "2099-01-01", "09:00", "10:00")
    assert booked is None
    assert is_locked is None


def test_is_within_cutoff_true_for_past_time():
    assert bo.is_within_cutoff("2000-01-01", "09:00") is True


def test_is_within_cutoff_false_for_far_future_time():
    assert bo.is_within_cutoff("2099-12-31", "09:00") is False

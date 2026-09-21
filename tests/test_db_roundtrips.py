"""
งบจำนวนรอบ DB ต่อ 1 request (performance guard rail)

ต้นทุนหลักของ API บน production คือ "รอบที่ backend คุยกับ MongoDB Atlas" (ราว 30-170 ms ต่อรอบ ขึ้นกับ
ระยะห่างระหว่าง Railway กับ Atlas) ไม่ใช่เวลา CPU ของ Python ดังนั้นการ refactor ห้ามทำให้ endpoint ใดใช้
รอบ DB เพิ่มขึ้น เทสต์นี้ล็อก "จำนวนคำสั่ง DB สูงสุด" ของแต่ละ endpoint หลัก (นับทุก find/find_one/
count_documents/update/insert/delete ที่ยิงลง collection) จากค่าที่วัดได้ ณ ตอนตั้งงบ

- นับ "จำนวนคำสั่ง" ไม่ใช่ความลึกของลำดับ: query ที่รันพร้อมกันด้วย common.parallel ยังถูกนับทุกตัว
- งบเป็นเพดาน (<=): ลดจำนวนรอบได้ แต่เพิ่มไม่ได้ ถ้าตั้งใจให้เพิ่มจริงต้องแก้งบในไฟล์นี้พร้อมเหตุผล
- วัดตอน cache ผู้ใช้ว่าง (cold) เพื่อรวมการค้น UserProfile ของ verify_user_token ด้วย
- ตั้ง DB_BUDGET_RECORD=1 เพื่อพิมพ์ค่าที่วัดได้แทนการตรวจ (ใช้ตอนปรับงบโดยตั้งใจ)
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.jwt_utils import encode_token
from tests.test_booking_flows import _Client as _ArrayFilterClient

RECORD = bool(os.getenv("DB_BUDGET_RECORD"))

FUTURE = "2099-01-01"
FUTURE2 = "2099-02-01"
OPS = {"find", "find_one", "count_documents", "update_one", "update_many", "insert_one", "insert_many",
       "delete_one", "delete_many", "aggregate", "replace_one", "estimated_document_count"}


# ── ตัวนับคำสั่ง DB ──────────────────────────────────────────────────────────

class _CountingCollection:
    def __init__(self, col, name, log):
        self._col, self._name, self._log = col, name, log

    def __getattr__(self, attr):
        real = getattr(self._col, attr)
        if attr not in OPS:
            return real

        def counted(*a, **kw):
            self._log.append(f"{self._name}.{attr}")
            return real(*a, **kw)

        return counted


class _CountingDB:
    def __init__(self, db, log):
        self._db, self._log = db, log

    def __getitem__(self, name):
        return _CountingCollection(self._db[name], name, self._log)


class _CountingClient:
    def __init__(self, client, log):
        self._client, self._log = client, log

    def __getitem__(self, name):
        return _CountingDB(self._client[name], self._log)


# ── สภาพแวดล้อมทดสอบ ───────────────────────────────────────────────────────────

def _token(user_id):
    return encode_token({"user_id": user_id}, timedelta(days=1))


def _seed(mongo, booking_status="Approved"):
    db = mongo["BORC"]
    now = datetime.now(timezone.utc)
    for uid, role in [("student-1", "Student"), ("student-2", "Student"), ("advisor-1", "Advisor")]:
        db["UserProfile"].insert_one({"userId": uid, "Role": role, "Status": "Approved", "Prefix": "นาย",
                                      "Firstname": uid, "Lastname": "ทดสอบ", "imageURL": ""})
    db["BookingOnline"].insert_one({
        "UserId": "student-1", "StudentName": "นายstudent-1 ทดสอบ", "AdvisorId": "advisor-1",
        "Advisor_Name": "อ.ทดสอบ", "Date": FUTURE, "Time": "09:00-10:00", "ResearchTopic": "T", "ResearchDetail": "D",
        "Status": booking_status, "RescheduledOnce": False, "AdvisorRescheduledOnce": False,
        "CreatedAt": now, "UpdatedAt": now,
    })
    db["ManageTimeSlots"].insert_one({
        "advisorId": "advisor-1", "advisor_name": "อ.ทดสอบ", "months": ["2099-01", "2099-02"],
        "dates": {
            FUTURE: [{"start": "09:00", "end": "10:00", "label": "", "booked": 1, "isLocked": True,
                      "is_closed": True, "max_booking": 1}],
            FUTURE2: [{"start": "10:00", "end": "11:00", "label": "", "booked": 0, "isLocked": False,
                       "is_closed": False, "max_booking": 1},
                      {"start": "13:00", "end": "14:00", "label": "", "booked": 0, "isLocked": False,
                       "is_closed": False, "max_booking": 1}],
        },
    })
    db["ApprovedHistory"].insert_one({"UserId": "student-1", "AdvisorId": "advisor-1", "AdvisorName": "อ.ทดสอบ",
                                      "Status": "Approved", "CreatedAt": now, "UpdatedAt": now})
    db["RescheduleHistory"].insert_one({"rescheduledById": "student-1", "rescheduledByRole": "Student",
                                        "advisorName": "อ.ทดสอบ", "status": "Rescheduled", "createdAt": now})
    db["CancelBookingHistory"].insert_one({"cancelledById": "student-1", "cancelledByRole": "Student",
                                           "advisorName": "อ.ทดสอบ", "status": "Cancelled", "createdAt": now})
    db["QueueManagementHistory"].insert_one({"userId": "advisor-1", "status": "Completed", "createdAt": now})


@pytest.fixture
def world(mongo_client, monkeypatch):
    import requests

    from users.auth import authUser
    from users.router.Advisor import (ConsultationAvailability, ManageQueueAdvisor, ManageTimeSlots,
                                      QueuehistoryAdvisor, Rechedule_Advisor, Show_Consult)
    from users.router.Students import (BookingOnline, ManageQueueStudent, QueuehistoryStudent, Reschedule_Students,
                                       Show_BookingData, ViewConsultationHours)

    log = []
    client = _CountingClient(_ArrayFilterClient(mongo_client), log)  # emulate array_filters (mongomock ไม่รองรับ)

    modules = [authUser, BookingOnline, ManageQueueStudent, Reschedule_Students, ViewConsultationHours,
               Show_BookingData, QueuehistoryStudent, ManageTimeSlots, ManageQueueAdvisor, Rechedule_Advisor,
               ConsultationAvailability, Show_Consult, QueuehistoryAdvisor]
    for mod in modules:
        monkeypatch.setattr(mod, "Connect_MongoDB", lambda: client)
    # ไม่ยิงออกเครือข่ายจริง (แจ้งเตือน ChatBot/LINE)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: type("R", (), {"status_code": 200, "json": lambda s: {}})())

    app = FastAPI()
    app.include_router(authUser.router, prefix="/authUser")
    app.include_router(Show_BookingData.router, prefix="/GETDataBookingOnline")
    app.include_router(BookingOnline.router, prefix="/booking")
    app.include_router(ManageQueueStudent.router, prefix="/manage-queue")
    app.include_router(Reschedule_Students.router, prefix="/reschedule")
    app.include_router(ViewConsultationHours.router, prefix="/advisor-slots")
    app.include_router(QueuehistoryStudent.router, prefix="/QueueHistoryStudent")
    app.include_router(ManageTimeSlots.router, prefix="/ManageTimeSlots")
    app.include_router(ManageQueueAdvisor.router, prefix="/advisor-queue")
    app.include_router(Rechedule_Advisor.router, prefix="/advisor-reschedule")
    app.include_router(Show_Consult.router, prefix="/advisor")
    app.include_router(ConsultationAvailability.router, prefix="/advisor-schedule")
    app.include_router(QueuehistoryAdvisor.router, prefix="/QueuehistoryAdvisor")
    return TestClient(app), mongo_client, log


# ── รายการ endpoint ─────────────────────────────────────────────────────
# (ชื่อ, ผู้ใช้, method, path, ตัวเลือก request, สถานะคิวตั้งต้น)

FORM_BOOK = {"advisor_id": "advisor-1", "advisor_name": "x", "date": FUTURE2, "time": "10:00-11:00",
             "research_topic": "หัวข้อ", "research_detail": "รายละเอียด"}
FORM_RESCHEDULE = {"new_date": FUTURE2, "new_start": "10:00", "new_end": "11:00", "new_label": "MORNING", "reason": "x"}

SCENARIOS = [
    # ── auth ──
    ("auth Me", "student-1", "GET", "/authUser/Me", {}, "Approved"),
    ("auth NavbarUsers", "student-1", "GET", "/authUser/NavbarUsers", {}, "Approved"),
    # ── นักศึกษา: หน้าแรก / ข้อมูล ──
    ("student ShowData", "student-1", "GET", "/GETDataBookingOnline/ShowData", {}, "Approved"),
    ("student BookingStats", "student-1", "GET", "/GETDataBookingOnline/BookingStats", {}, "Approved"),
    ("student History", "student-1", "GET", "/QueueHistoryStudent/History", {}, "Approved"),
    # ── นักศึกษา: จอง ──
    ("student AvailableAdvisors", "student-1", "GET", "/booking/AvailableAdvisors", {}, "Approved"),
    ("student AvailableSlots", "student-1", "GET", "/booking/AvailableSlots/advisor-1", {}, "Approved"),
    ("student BookingStatus", "student-1", "GET", "/booking/BookingStatus", {}, "Approved"),
    ("student BookingOnline (POST)", "student-2", "POST", "/booking/BookingOnline", {"data": FORM_BOOK}, "Approved"),
    # ── นักศึกษา: จัดการคิว ──
    ("student MyBookingDetail", "student-1", "GET", "/manage-queue/MyBookingDetail", {}, "Approved"),
    ("student CheckRescheduleEligibility", "student-1", "GET", "/manage-queue/CheckRescheduleEligibility", {}, "Approved"),
    ("student CancelBooking", "student-1", "DELETE", "/manage-queue/CancelBooking", {"json": {"cancelReason": "x"}}, "Pending"),
    # ── นักศึกษา: เลื่อนคิว ──
    ("student reschedule BookingInfo", "student-1", "GET", "/reschedule/BookingInfo", {}, "Approved"),
    ("student reschedule AvailableSlots", "student-1", "GET", "/reschedule/AvailableSlots", {}, "Approved"),
    ("student reschedule RescheduleBooking", "student-1", "PUT", "/reschedule/RescheduleBooking", {"data": FORM_RESCHEDULE}, "Approved"),
    # ── นักศึกษา: ดูช่วงเวลาอาจารย์ ──
    ("student ViewConsultationHours Advisors", "student-1", "GET", "/advisor-slots/Advisors", {}, "Approved"),
    ("student ViewConsultationHours Slots", "student-1", "GET", "/advisor-slots/Slots/advisor-1", {}, "Approved"),
    # ── อาจารย์: จัดการช่วงเวลา ──
    ("advisor TimeSlots", "advisor-1", "GET", "/ManageTimeSlots/TimeSlots", {}, "Approved"),
    ("advisor MyTimeSlots/date", "advisor-1", "GET", f"/ManageTimeSlots/MyTimeSlots/{FUTURE2}", {}, "Approved"),
    ("advisor SaveTimeSlots", "advisor-1", "POST", "/ManageTimeSlots/SaveTimeSlots",
     {"json": {"dates": {"2099-03-01": [{"start": "09:00", "end": "10:00", "max_booking": 1}]}}}, "Approved"),
    ("advisor CopyToAllDays", "advisor-1", "POST", "/ManageTimeSlots/CopyToAllDays",
     {"json": {"target_month": "2099-03", "slots": [{"start": "09:00", "end": "09:30"}]}}, "Approved"),
    ("advisor UpdateTimeSlots", "advisor-1", "PUT", f"/ManageTimeSlots/UpdateTimeSlots/{FUTURE2}",
     {"json": {"old_start": "10:00", "old_end": "11:00", "new_start": "10:30", "new_end": "11:30"}}, "Approved"),
    ("advisor DeleteTimeSlots", "advisor-1", "DELETE", f"/ManageTimeSlots/DeleteTimeSlots/{FUTURE2}",
     {"json": {"start": "13:00", "end": "14:00"}}, "Approved"),
    # ── อาจารย์: จัดการคิว ──
    ("advisor AdvisorQueues", "advisor-1", "GET", "/advisor-queue/AdvisorQueues", {}, "Approved"),
    ("advisor ConfirmQueue", "advisor-1", "PUT", "/advisor-queue/ConfirmQueue", {"json": {"user_id": "student-1"}}, "Pending"),
    ("advisor AdvisorCancelQueue", "advisor-1", "DELETE", "/advisor-queue/AdvisorCancelQueue",
     {"json": {"user_id": "student-1", "reason": "x"}}, "Approved"),
    # ── อาจารย์: เลื่อนคิว ──
    ("advisor reschedule BookingInfo", "advisor-1", "GET", "/advisor-reschedule/BookingInfo", {"params": {"user_id": "student-1"}}, "Approved"),
    ("advisor reschedule AvailableSlots", "advisor-1", "GET", "/advisor-reschedule/AvailableSlots", {"params": {"user_id": "student-1"}}, "Approved"),
    ("advisor reschedule RescheduleBooking", "advisor-1", "PUT", "/advisor-reschedule/RescheduleBooking",
     {"json": {"user_id": "student-1", "new_date": FUTURE2, "new_start": "10:00", "new_end": "11:00", "reason": "x"}}, "Approved"),
    # ── อาจารย์: เปิด-ปิดช่วงเวลา ──
    ("advisor GetDaySlots", "advisor-1", "GET", f"/advisor-schedule/GetDaySlots/{FUTURE2}", {}, "Approved"),
    ("advisor SaveDaySchedule", "advisor-1", "PUT", "/advisor-schedule/SaveDaySchedule",
     {"json": {"date": FUTURE2, "day_closed": False, "slots": [{"start": "10:00", "end": "11:00", "is_closed": True},
                                                                 {"start": "13:00", "end": "14:00", "is_closed": False}]}}, "Approved"),
    # ── อาจารย์: หน้าแรก / ประวัติ ──
    ("advisor TodayQueue", "advisor-1", "GET", "/advisor/TodayQueue", {}, "Approved"),
    ("advisor AdvisorStats", "advisor-1", "GET", "/advisor/AdvisorStats", {}, "Approved"),
    ("advisor QueuehistoryAdvisor All", "advisor-1", "GET", "/QueuehistoryAdvisor/All", {}, "Approved"),
]


# งบ = จำนวนคำสั่ง DB สูงสุดต่อ request (วัดจากโค้ดจริงตอนตั้งงบ; ตั้ง DB_BUDGET_RECORD=1 เพื่อวัดใหม่)
BUDGET = {
    "auth Me": 1,
    "auth NavbarUsers": 2,
    "student ShowData": 2,
    "student BookingStats": 3,
    "student History": 5,
    "student AvailableAdvisors": 2,
    "student AvailableSlots": 2,
    "student BookingStatus": 2,
    "student BookingOnline (POST)": 9,
    "student MyBookingDetail": 2,
    "student CheckRescheduleEligibility": 2,
    "student CancelBooking": 8,
    "student reschedule BookingInfo": 2,
    "student reschedule AvailableSlots": 3,
    "student reschedule RescheduleBooking": 10,
    "student ViewConsultationHours Advisors": 2,
    "student ViewConsultationHours Slots": 2,
    "advisor TimeSlots": 2,
    "advisor MyTimeSlots/date": 2,
    "advisor SaveTimeSlots": 3,
    "advisor CopyToAllDays": 3,
    "advisor UpdateTimeSlots": 3,
    "advisor DeleteTimeSlots": 3,
    "advisor AdvisorQueues": 3,
    "advisor ConfirmQueue": 5,
    "advisor AdvisorCancelQueue": 8,
    "advisor reschedule BookingInfo": 2,
    "advisor reschedule AvailableSlots": 3,
    "advisor reschedule RescheduleBooking": 10,
    "advisor GetDaySlots": 4,
    "advisor SaveDaySchedule": 5,
    "advisor TodayQueue": 2,
    "advisor AdvisorStats": 5,
    "advisor QueuehistoryAdvisor All": 2,
}


@pytest.mark.parametrize("name,user,method,path,kw,booking_status", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_endpoint_stays_within_db_round_trip_budget(world, name, user, method, path, kw, booking_status):
    budget = BUDGET[name]
    from users.auth import authUser

    client, mongo, log = world
    _seed(mongo, booking_status)
    authUser.invalidate_user_cache()  # cold: นับการค้น UserProfile ของ verify_user_token ด้วย
    log.clear()

    resp = client.request(method, path, cookies={"access_token": _token(user)}, **kw)

    used = len(log)
    if RECORD:
        print(f"RECORD|{name}|{used}|{resp.status_code}")
        return
    assert resp.status_code == 200, f"{name}: HTTP {resp.status_code} {resp.text[:200]}"
    assert used <= budget, (
        f"{name}: ใช้ DB {used} คำสั่ง เกินงบ {budget} — ตรวจว่า refactor เพิ่ม query หรือไม่ "
        f"(คำสั่งที่ใช้: {', '.join(log)})"
    )

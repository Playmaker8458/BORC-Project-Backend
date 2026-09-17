import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel
from typing import List

router = APIRouter()

ACTIVE_STATUSES = ["Pending", "Approved", "InProgress", "Rescheduled"]


class SlotToggle(BaseModel):
    start    : str
    end      : str
    is_closed: bool


class SaveScheduleBody(BaseModel):
    date      : str
    day_closed: bool = False
    slots     : List[SlotToggle]


# ---------------------------------------------------------------------------
# GET /advisor-schedule/GetDaySlots/{date}
# ---------------------------------------------------------------------------
@router.get("/GetDaySlots/{date}")
async def get_day_slots(date: str, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)

        db     = Connect_MongoDB()["BORC"]
        ts_col = db["ManageTimeSlots"]
        av_col = db["ConsultationAvailability"]

        # ✅ ดึงทุก document ของ advisor นี้ แล้วหา date ที่ต้องการ
        #    เพราะ dates อาจกระจายอยู่หลาย document (แยกตาม month)
        #    ไม่ใช้ find_one เพราะอาจเจอ document ที่ไม่มี date นั้น
        all_docs = list(ts_col.find(
            {"advisorId": advisor_id},
            {"_id": 0, f"dates.{date}": 1}
        ))

        # รวม slots จากทุก document (กรณี date อยู่คนละ doc)
        slots = []
        for doc in all_docs:
            day_slots = doc.get("dates", {}).get(date, [])
            if day_slots:
                slots = day_slots
                break   # เจอแล้วหยุด


        if not slots:
            return {"date": date, "day_closed": False, "slots": []}

        # ดึงสถานะจาก ConsultationAvailability (ถ้ามี)
        av_doc = av_col.find_one(
            {"advisorId": advisor_id, "date": date},
            {"_id": 0, "day_closed": 1, "slots": 1}
        )

        day_closed  = av_doc.get("day_closed", False) if av_doc else False
        av_slot_map = {}
        if av_doc:
            for s in av_doc.get("slots", []):
                key = f"{s['start']}-{s['end']}"
                av_slot_map[key] = s.get("is_closed", False)

        # ดึง booking active ในวันนั้น
        booking_col  = db["BookingOnline"]
        active_books = list(booking_col.find(
            {"AdvisorId": advisor_id, "Date": date, "Status": {"$in": ACTIVE_STATUSES}},
            {"_id": 0, "Time": 1}
        ))

        booked_times = set()
        for b in active_books:
            if b.get("Time"):
                booked_times.add(b["Time"].replace(" ", ""))

        result_slots = []
        for s in slots:
            key         = f"{s['start']}-{s['end']}"
            has_booking = key in booked_times
            is_closed   = av_slot_map.get(key, s.get("is_closed", False))

            result_slots.append({
                "start"      : s["start"],
                "end"        : s["end"],
                "label"      : s.get("label", ""),
                "max_booking": s.get("max_booking", 1),
                "booked"     : s.get("booked", 0),
                "is_closed"  : is_closed,
                "has_booking": has_booking,
            })

        return {"date": date, "day_closed": day_closed, "slots": result_slots}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[GetDaySlots] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# PUT /advisor-schedule/SaveDaySchedule
# ---------------------------------------------------------------------------
@router.put("/SaveDaySchedule")
async def save_day_schedule(body: SaveScheduleBody, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)

        db     = Connect_MongoDB()["BORC"]
        ts_col = db["ManageTimeSlots"]
        av_col = db["ConsultationAvailability"]

        # ✅ ค้นหา document ที่มี dates.{body.date} อยู่จริง (scan ทุก doc ของ advisor)
        target_doc = None
        for doc in ts_col.find({"advisorId": advisor_id}, {"_id": 1, f"dates.{body.date}": 1}):
            if doc.get("dates", {}).get(body.date):
                target_doc = doc
                break

        if not target_doc:
            raise HTTPException(status_code=404, detail=f"ไม่พบ slot สำหรับวันที่ {body.date}")

        existing_slots = target_doc.get("dates", {}).get(body.date, [])
        doc_id         = target_doc["_id"]   # ✅ ใช้ _id เพื่อ update ถูก document

        # ดึง booking active
        booking_col  = db["BookingOnline"]
        active_books = list(booking_col.find(
            {"AdvisorId": advisor_id, "Date": body.date, "Status": {"$in": ACTIVE_STATUSES}},
            {"_id": 0, "Time": 1}
        ))

        booked_times = set()
        for b in active_books:
            if b.get("Time"):
                booked_times.add(b["Time"].replace(" ", ""))

        slot_map = {f"{s.start}-{s.end}": s.is_closed for s in body.slots}
        now      = datetime.now(timezone.utc)

        # ── อัปเดต ManageTimeSlots ────────────────────────────────────────────
        updated_ts_slots = []
        for s in existing_slots:
            key         = f"{s['start']}-{s['end']}"
            has_booking = key in booked_times

            if has_booking:
                updated_ts_slots.append(s)
                continue

            want_close = True if body.day_closed else slot_map.get(key, s.get("is_closed", False))

            if want_close:
                updated_s = {**s, "is_closed": True, "isLocked": True, "booked": 0}
            else:
                updated_s = {k: v for k, v in s.items() if k != "is_closed"}
                updated_s["isLocked"] = False
                updated_s["booked"]   = s.get("booked", 0)

            updated_ts_slots.append(updated_s)

        # ✅ update ด้วย _id ของ document ที่เจอ ป้องกันอัปเดตผิด document
        ts_col.update_one(
            {"_id": doc_id},
            {"$set": {
                f"dates.{body.date}": updated_ts_slots,
                "updatedAt"         : now,
            }}
        )

        # ── บันทึกลง ConsultationAvailability ────────────────────────────────
        av_slots = []
        for s in updated_ts_slots:
            av_slots.append({
                "start"    : s["start"],
                "end"      : s["end"],
                "label"    : s.get("label", ""),
                "is_closed": s.get("is_closed", False),
                "isLocked" : s.get("isLocked", False),
                "booked"   : s.get("booked", 0),
            })

        av_col.update_one(
            {"advisorId": advisor_id, "date": body.date},
            {
                "$set": {
                    "advisorId" : advisor_id,
                    "date"      : body.date,
                    "day_closed": body.day_closed,
                    "slots"     : av_slots,
                    "updatedAt" : now,
                },
                "$setOnInsert": {"createdAt": now},
            },
            upsert=True
        )

        return {"message": "บันทึกการตั้งค่าสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[SaveDaySchedule] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")
import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone
from common.booking_status import ACTIVE_STATUSES
from common.slot_service import validate_date_or_400
from common.time_slot_rules import slot_closed_by_advisor
from pydantic import BaseModel
from typing import List

router = APIRouter()


class SlotToggle(BaseModel):
    start    : str
    end      : str
    is_closed: bool


class SaveScheduleBody(BaseModel):
    # เปิด-ปิดทีละช่วงเวลาเท่านั้น (เลิกใช้ "ปิดทั้งวัน" / day_closed แล้ว — client เก่าที่ยังส่งมาจะถูกละไว้)
    date : str
    slots: List[SlotToggle]


# ---------------------------------------------------------------------------
# GET /advisor-schedule/GetDaySlots/{date}
# ---------------------------------------------------------------------------
@router.get("/GetDaySlots/{date}")
def get_day_slots(date: str, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        validate_date_or_400(date)  # ใช้ประกอบ key `dates.{date}` — ห้ามรับรูปแบบแปลก

        db     = Connect_MongoDB()["BORC"]
        ts_col = db["ManageTimeSlots"]

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
            return {"date": date, "slots": []}

        # สถานะปิดอ่านจาก ManageTimeSlots เท่านั้น — ConsultationAvailability เป็นแค่บันทึกประวัติการตั้งค่า
        # (เดิมเชื่อสำเนาใน ConsultationAvailability ก่อน ซึ่งค้างเป็น "ปิด" หลังนักศึกษายกเลิกคิว
        # หรือหลัง Copy ทับวันนั้น แล้วการกดบันทึกซ้ำจะปิด slot จริงโดยไม่ตั้งใจ)

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
            has_booking = key in booked_times or s.get("booked", 0) >= 1
            # การจองก็ตั้ง is_closed=True — นับเป็น "ปิด" เฉพาะที่อาจารย์ปิดเอง
            is_closed   = not has_booking and slot_closed_by_advisor(s)

            result_slots.append({
                "start"      : s["start"],
                "end"        : s["end"],
                "label"      : s.get("label", ""),
                "max_booking": s.get("max_booking", 1),
                "booked"     : s.get("booked", 0),
                "is_closed"  : is_closed,
                "has_booking": has_booking,
            })

        return {"date": date, "slots": result_slots}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[GetDaySlots] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# PUT /advisor-schedule/SaveDaySchedule
# ---------------------------------------------------------------------------
@router.put("/SaveDaySchedule")
def save_day_schedule(body: SaveScheduleBody, request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)
        validate_date_or_400(body.date)  # ใช้ประกอบ key `dates.{date}` — ห้ามรับรูปแบบแปลก

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
            # slot ที่ถูกจองล็อกไว้ (booked) ก็ห้ามแตะ แม้จะยังไม่เห็นคิวใน BookingOnline
            has_booking = key in booked_times or s.get("booked", 0) >= 1

            if has_booking:
                updated_ts_slots.append(s)
                continue

            want_close = slot_map.get(key, slot_closed_by_advisor(s))

            # ปิดใช้แค่ is_closed — ไม่ตั้ง isLocked (ล็อก = มีคนจอง) อาจารย์จึงยังแก้/ลบ slot ที่ปิดไว้ได้
            if want_close:
                updated_s = {**s, "is_closed": True, "isLocked": False, "booked": 0}
            else:
                updated_s = {k: v for k, v in s.items() if k != "is_closed"}
                updated_s["isLocked"] = False
                updated_s["booked"]   = s.get("booked", 0)

            updated_ts_slots.append(updated_s)

        # ✅ update ด้วย _id ของ document ที่เจอ ป้องกันอัปเดตผิด document
        # เขียนเมื่อวันนั้นยังเท่ากับที่อ่านมา: ถ้านักศึกษาจอง (ล็อก slot) ตัดหน้าระหว่างนี้
        # การเขียนทั้ง array กลับจะทับ lock ทิ้ง จึงตอบ 409 ให้ผู้ใช้ลองใหม่แทน
        saved = ts_col.update_one(
            {"_id": doc_id, f"dates.{body.date}": existing_slots},
            {"$set": {
                f"dates.{body.date}": updated_ts_slots,
                "updatedAt"         : now,
            }}
        )
        if saved.matched_count == 0:
            raise HTTPException(
                status_code=409,
                detail="ช่วงเวลามีการเปลี่ยนแปลงพร้อมกัน (เช่น มีนักศึกษาจอง) กรุณาลองใหม่อีกครั้ง",
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
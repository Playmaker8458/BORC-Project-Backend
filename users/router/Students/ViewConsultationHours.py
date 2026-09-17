import logging

from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token
from datetime import datetime, timezone, timedelta

router = APIRouter()
logger = logging.getLogger(__name__)


def get_today_str() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d")


# ─── GET /advisor-slots/Advisors ─────────────────────────────────────────────
# ดึงรายชื่ออาจารย์ทั้งหมดที่มี slot ว่าง
@router.get("/Advisors")
async def get_advisors(request: Request):
    try:
        verify_user_token(request)
        db       = Connect_MongoDB()["BORC"]
        today    = get_today_str()

        docs = list(db["ManageTimeSlots"].find(
            {},
            {"_id": 0, "advisorId": 1, "advisor_name": 1, "dates": 1, "months": 1}
        ))

        result = []
        for doc in docs:
            advisor_id   = doc.get("advisorId", "")
            advisor_name = doc.get("advisor_name", "")
            months       = doc.get("months", [])
            dates        = doc.get("dates", {})

            # นับเฉพาะวันที่ยังไม่ผ่านมา และมี slot ว่างอย่างน้อย 1
            available_dates = [
                d for d, slots in dates.items()
                if d >= today and any(
                    not s.get("isLocked", False) and not s.get("is_closed", False)
                    for s in slots
                )
            ]

            if not available_dates:
                continue

            result.append({
                "advisorId"      : advisor_id,
                "advisorName"    : advisor_name,
                "availableMonths": len(months),
                "availableDates" : len(available_dates),
            })

        return {"advisors": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /advisor-slots/Slots/{advisor_id} ────────────────────────────────────
# ดึง slot ทั้งหมดของอาจารย์คนนั้น พร้อมสถานะ
@router.get("/Slots/{advisor_id}")
async def get_advisor_slots(advisor_id: str, request: Request):
    try:
        verify_user_token(request)
        db    = Connect_MongoDB()["BORC"]
        today = get_today_str()

        docs = list(db["ManageTimeSlots"].find(
            {"advisorId": advisor_id},
            {"_id": 0, "advisor_name": 1, "dates": 1}
        ))

        if not docs:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลอาจารย์")

        advisor_name = docs[0].get("advisor_name", "")
        merged_dates = {}

        for doc in docs:
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list) or date < today:
                    continue

                slot_list = []
                for s in slots:
                    is_locked   = s.get("isLocked", False)
                    is_closed   = s.get("is_closed", False)
                    booked      = s.get("booked", 0)
                    max_booking = s.get("max_booking", 1)

                    if is_closed and not is_locked:
                        status = "ปิด"
                    elif is_locked or booked >= max_booking:
                        status = "เต็ม"
                    else:
                        status = "ว่าง"

                    slot_list.append({
                        "start" : s["start"],
                        "end"   : s["end"],
                        "label" : s.get("label", ""),
                        "status": status,
                    })

                if slot_list:
                    merged_dates[date] = slot_list

        # เรียงวันที่
        sorted_dates = dict(sorted(merged_dates.items()))

        return {
            "advisorId"  : advisor_id,
            "advisorName": advisor_name,
            "dates"      : sorted_dates,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
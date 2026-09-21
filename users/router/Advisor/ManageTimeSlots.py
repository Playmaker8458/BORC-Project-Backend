import logging
import re
import calendar
from fastapi import APIRouter, HTTPException, Request
from pymongo.errors import DuplicateKeyError
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import get_current_advisor
from datetime import datetime, timezone, timedelta, date as date_type

from common.slot_service import get_today_str
from common.time_slot_rules import (
    MAX_SLOTS_PER_DAY,
    CopyToAllDaysRequest,
    DeleteSlotRequest,
    ManageTimeSlotsRequest,
    UpdateSlotRequest,
    check_overlap,
    compute_is_locked,
    is_slot,
    merge_months,
    new_slot,
    preserving_slot,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def get_today_utc7() -> date_type:
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date_or_400(date: str) -> date_type:
    """ตรวจรูปแบบ YYYY-MM-DD และว่าเป็นวันที่มีอยู่จริง คืนค่า date หรือ raise 400"""
    if not _DATE_PATTERN.match(date):
        raise HTTPException(status_code=400, detail="รูปแบบวันที่ไม่ถูกต้อง (YYYY-MM-DD)")
    try:
        return datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="วันที่ไม่ถูกต้อง")


def validate_date_format_read(date: str) -> None:
    """ตรวจรูปแบบวันที่เท่านั้น — ใช้กับ GET"""
    _parse_date_or_400(date)


def validate_date_format_write(date: str) -> None:
    """ตรวจรูปแบบ + ห้ามวันที่ผ่านมาแล้ว — ใช้กับ POST (สร้างใหม่) เท่านั้น

    วันนี้ยังกำหนดได้ (เพื่อให้ทดสอบระบบด้วยวันเดียวกันได้) ห้ามเฉพาะวันก่อนหน้า
    """
    if _parse_date_or_400(date) < get_today_utc7():
        raise HTTPException(
            status_code=400,
            detail="ไม่สามารถกำหนดช่วงเวลาวันที่ผ่านมาได้"
        )


def validate_date_format_delete_update(date: str) -> None:
    """
    ตรวจรูปแบบวันที่เท่านั้น ไม่บล็อกวันที่ผ่านมา
    ใช้กับ DELETE / UPDATE เพราะ slot เดิมอาจถูกสร้างไว้ก่อนหน้า
    และยังต้องแก้ไข/ลบได้แม้วันนั้นจะผ่านไปแล้ว
    """
    _parse_date_or_400(date)


def validate_month_format(month: str) -> None:
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="รูปแบบเดือนไม่ถูกต้อง (YYYY-MM)")


def _raise_if_too_many_slots(date: str, count: int) -> None:
    """raise 400 ถ้าจำนวนช่วงเวลาของวันนั้นเกิน MAX_SLOTS_PER_DAY"""
    if count > MAX_SLOTS_PER_DAY:
        raise HTTPException(
            status_code=400,
            detail=f"วันที่ {date} ตั้งช่วงเวลาได้ไม่เกิน {MAX_SLOTS_PER_DAY} ช่วงต่อวัน"
        )


def _view_slot(s: dict) -> dict:
    """slot ที่ส่งให้อาจารย์ดู: max_booking เป็น 1 เสมอ และ isLocked เป็นจริงเมื่อมีคนจองแล้ว"""
    return {**s, "max_booking": 1, "isLocked": compute_is_locked(s.get("booked", 0), s.get("isLocked", False))}


def get_db_collection():
    try:
        db = Connect_MongoDB()["BORC"]
        return db["ManageTimeSlots"]
    except Exception:
        logger.exception("ไม่สามารถเชื่อมต่อฐานข้อมูลได้")
        raise HTTPException(status_code=500, detail="ไม่สามารถเชื่อมต่อฐานข้อมูลได้")


def advisor_filter(payload: dict) -> dict:
    return {"advisorId": payload["user_id"]}


def create_timeslot_indexes(db):
    """สร้าง index ของ ManageTimeSlots — ตอนนี้เรียกจาก main.lifespan ผ่าน common/indexes.ensure_unique_indexes"""
    from common.indexes import ensure_unique_indexes
    ensure_unique_indexes(db)


# ─────────────────────────────────────────
# GET /MyTimeSlots/{date}
# ─────────────────────────────────────────
@router.get("/MyTimeSlots/{date}")
def get_my_time_slots(date: str, request: Request):
    try:
        validate_date_format_read(date)
        payload = get_current_advisor(request)

        col = get_db_collection()
        doc = col.find_one(
            advisor_filter(payload),
            {"_id": 0, f"dates.{date}": 1}
        )

        if not doc:
            return {"data": {"slots": []}}

        slots = doc.get("dates", {}).get(date, [])
        return {"data": {"slots": [_view_slot(s) for s in slots]}}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")


# ─────────────────────────────────────────
# GET /MyTimeSlots/month/{month}
# ─────────────────────────────────────────
@router.get("/MyTimeSlots/month/{month}")
def get_my_time_slots_by_month(month: str, request: Request):
    try:
        validate_month_format(month)
        payload = get_current_advisor(request)

        col = get_db_collection()
        doc = col.find_one(
            advisor_filter(payload),
            {"_id": 0, "dates": 1}
        )

        if not doc:
            return {"data": {"month": month, "dates": {}}}

        all_dates = doc.get("dates", {})
        filtered  = {
            d: [_view_slot(s) for s in v]
            for d, v in all_dates.items() if d.startswith(month)
        }

        return {"data": {"month": month, "dates": filtered}}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")


# ─────────────────────────────────────────
# GET /TimeSlots
# ─────────────────────────────────────────
@router.get("/TimeSlots")
def get_time_slots(request: Request):
    try:
        payload = get_current_advisor(request)
        today   = get_today_str()
        col     = get_db_collection()

        doc = col.find_one(
            advisor_filter(payload),
            {"_id": 0, "advisor_name": 1, "advisorId": 1, "dates": 1}
        )

        if not doc:
            # อาจารย์ใหม่ยังไม่เคยสร้าง slot: คืน schema ปกติเพื่อให้หน้าเว็บใช้งานได้
            return {
                "advisorId": payload["user_id"],
                "advisorName": f"{payload.get('Prefix', '')}{payload.get('Firstname', '')} {payload.get('Lastname', '')}".strip(),
                "dates": {},
            }

        merged_dates = {}
        for date, slots in doc.get("dates", {}).items():
            if not isinstance(slots, list) or date < today:
                continue

            slot_list = []
            for s in slots:
                is_locked   = s.get("isLocked", False)
                is_closed   = s.get("is_closed", False)
                booked      = s.get("booked", 0)
                max_booking = s.get("max_booking", 1)

                if is_locked or booked >= max_booking:
                    status = "เต็ม"
                elif is_closed:
                    status = "ปิด"
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

        return {
            "advisorId"  : doc.get("advisorId", ""),
            "advisorName": doc.get("advisor_name", ""),
            "dates"      : dict(sorted(merged_dates.items())),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─────────────────────────────────────────
# Helpers: สร้าง/ค้นหา slot ที่ใช้ร่วมกันหลาย endpoint
# ─────────────────────────────────────────
def _upsert_advisor_document(col, payload: dict, set_fields: dict, now: datetime) -> None:
    """เขียนข้อมูล slot ของอาจารย์ (สร้างเอกสารใหม่ถ้ายังไม่มี) — advisorId/ชื่อ/createdAt ตั้งเฉพาะตอนสร้าง"""
    col.update_one(
        advisor_filter(payload),
        {
            "$set": set_fields,
            "$setOnInsert": {
                "advisorId"   : payload["user_id"],
                "advisor_name": f"{payload.get('Prefix','')}{payload.get('Firstname','')}"
                                f" {payload.get('Lastname','')}",
                "createdAt"   : now,
            },
        },
        upsert=True
    )


def _load_day_slots(col, payload: dict, date: str, extra_fields: dict | None = None) -> tuple[dict, list]:
    """ดึงเอกสารของอาจารย์ (เฉพาะวันที่ระบุ + ฟิลด์เสริม) คืน (เอกสาร, slot ของวันนั้น); raise 404 ถ้าไม่มี"""
    existing = col.find_one(
        advisor_filter(payload),
        {"_id": 0, f"dates.{date}": 1, **(extra_fields or {})}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลช่วงเวลา")

    old_slots = existing.get("dates", {}).get(date, [])
    if not old_slots:
        raise HTTPException(status_code=404, detail=f"ไม่พบช่วงเวลาของวันที่ {date}")
    return existing, old_slots


# ─────────────────────────────────────────
# เขียนแบบ compare-and-set: กันเขียนทับ booked/isLocked ที่นักศึกษาเพิ่งล็อกระหว่างที่อาจารย์แก้
# ─────────────────────────────────────────
MAX_WRITE_ATTEMPTS = 3


def _unchanged_days_filter(payload: dict, expected_days: dict) -> dict:
    """filter ที่ match เฉพาะเมื่อแต่ละวันยังเท่ากับที่อ่านมา (วันที่ไม่มีเลย = ไม่มี field หรือ array ว่าง)"""
    cond = {
        f"dates.{date}": slots if slots else {"$in": [None, []]}
        for date, slots in expected_days.items()
    }
    return {**advisor_filter(payload), **cond}


def _write_if_unchanged(col, payload: dict, expected_days: dict, update: dict) -> bool:
    """update เมื่อวันที่เกี่ยวข้องยังไม่ถูกแก้โดยคนอื่น; คืน False ถ้าถูกแก้ตัดหน้า (ให้อ่านใหม่แล้วลองซ้ำ)"""
    return col.update_one(_unchanged_days_filter(payload, expected_days), update).matched_count == 1


def _retry_write(attempt) -> None:
    """เรียก attempt() (อ่าน→คำนวณ→เขียน คืน True เมื่อเขียนสำเร็จ) ซ้ำได้ MAX_WRITE_ATTEMPTS ครั้ง ไม่งั้น 409"""
    for _ in range(MAX_WRITE_ATTEMPTS):
        if attempt():
            return
    raise HTTPException(
        status_code=409,
        detail="ช่วงเวลามีการเปลี่ยนแปลงพร้อมกัน (เช่น มีนักศึกษาจอง) กรุณาลองใหม่อีกครั้ง",
    )


def _find_unlocked_target(old_slots: list, date: str, start: str, end: str, action: str) -> dict:
    """หา slot ที่จะแก้/ลบ; raise 404 ถ้าไม่พบ, 400 ถ้าถูกล็อกแล้ว (action = "แก้ไข" หรือ "ลบ")"""
    target = next((s for s in old_slots if is_slot(s, start, end)), None)
    if not target:
        raise HTTPException(
            status_code=404,
            detail=f"ไม่พบช่วงเวลา {start}-{end} ในวันที่ {date}"
        )
    if target.get("isLocked", False):
        raise HTTPException(
            status_code=400,
            detail=f"ช่วงเวลา {start}-{end} ถูกล็อกแล้ว ไม่สามารถ{action}ได้"
        )
    return target


def _write_new_days(col, payload: dict, existing: dict | None, current_dates: dict, new_days: dict) -> bool:
    """เขียน slot ของหลายวัน (วัน -> slot) พร้อมอัปเดต months; คืน False ถ้าถูกแก้ตัดหน้า (ให้อ่านใหม่แล้วลองซ้ำ)"""
    now        = datetime.now(timezone.utc)
    set_fields = {"updatedAt": now}
    for date, day_slots in new_days.items():
        set_fields[f"dates.{date}"] = day_slots
    set_fields["months"] = merge_months(existing, list(current_dates.keys()) + list(new_days.keys()))

    if existing is None:  # อาจารย์ใหม่ ยังไม่มีเอกสารให้ compare → สร้างด้วย upsert
        try:
            _upsert_advisor_document(col, payload, set_fields, now)
        except DuplicateKeyError:
            # อีกคำขอสร้างเอกสารของอาจารย์คนนี้ตัดหน้า (unique advisorId): อ่านใหม่แล้วเขียนแบบ compare-and-set
            return False
        return True
    expected = {date: current_dates.get(date, []) for date in new_days}
    return _write_if_unchanged(col, payload, expected, {"$set": set_fields})


# ─────────────────────────────────────────
# POST /SaveTimeSlots
# ─────────────────────────────────────────
def _merge_day_slots(date: str, existing_slots: list, incoming: list) -> list:
    """รวม slot เดิมที่ไม่ได้ส่งมาอีก + slot ใหม่ของวันนั้น เรียงตามเวลาเริ่ม; raise 400 ถ้าซ้อนกัน

    คง slot เดิมที่ saved แล้วและไม่ได้อยู่ในรายการใหม่ไว้ทั้งหมด (ไม่ใช่เฉพาะที่มีคนจอง)
    """
    existing_map   = {(s["start"], s["end"]): s for s in existing_slots}
    incoming_times = {(s.start, s.end) for s in incoming}
    kept_slots     = [s for s in existing_slots if (s["start"], s["end"]) not in incoming_times]

    if check_overlap(kept_slots + [{"start": s.start, "end": s.end} for s in incoming]):
        raise HTTPException(
            status_code=400,
            detail=f"วันที่ {date} มีช่วงเวลาซ้อนกับช่วงเวลาที่มีอยู่แล้ว"
        )

    _raise_if_too_many_slots(date, len(kept_slots) + len(incoming_times))

    new_slots = [preserving_slot(s.start, s.end, existing_map.get((s.start, s.end))) for s in incoming]
    return sorted(kept_slots + new_slots, key=lambda s: s["start"])


@router.post("/SaveTimeSlots")
def save_time_slots(data: ManageTimeSlotsRequest, request: Request):
    try:
        if not data.dates:
            raise HTTPException(status_code=400, detail="กรุณาเพิ่มช่วงเวลาอย่างน้อย 1 วัน")

        for date in data.dates:
            validate_date_format_write(date)

        payload = get_current_advisor(request)
        col     = get_db_collection()

        def attempt() -> bool:
            existing      = col.find_one(advisor_filter(payload))
            current_dates = existing.get("dates", {}) if existing else {}

            new_days = {date: _merge_day_slots(date, current_dates.get(date, []), slots)
                        for date, slots in data.dates.items()}
            return _write_new_days(col, payload, existing, current_dates, new_days)

        _retry_write(attempt)

        return {"message": "บันทึกช่วงเวลาให้คำปรึกษาสำเร็จ"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(">>> UNEXPECTED ERROR in SaveTimeSlots: %s", e)
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")


# ─────────────────────────────────────────
# POST /CopyToAllDays
# ─────────────────────────────────────────
def _plan_copy_days(slots: list, year: int, month_num: int, days_in_month: int, today: date_type,
                    current_dates: dict) -> tuple[dict, int, list]:
    """เลือกวันในเดือนที่จะ copy slot ไปลง

    คืน (วัน -> slot ที่จะเขียน, จำนวนวันที่ข้าม, วันที่เดิมมี slot และจะถูกแทนที่)
    ข้ามวันที่ไม่ใช่วันหลังวันนี้ และวันที่มีนักศึกษาจองแล้ว
    """
    to_write: dict[str, list] = {}
    overwrite_days: list[str] = []
    skipped_days = 0

    for day in range(1, days_in_month + 1):
        date_str = f"{year}-{month_num:02d}-{day:02d}"
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            skipped_days += 1
            continue

        if dt <= today:
            skipped_days += 1
            continue

        existing_slots = current_dates.get(date_str, [])
        if any(s.get("booked", 0) > 0 for s in existing_slots):
            skipped_days += 1
            continue

        if existing_slots:
            overwrite_days.append(date_str)
        to_write[date_str] = [new_slot(s.start, s.end) for s in slots]

    return to_write, skipped_days, overwrite_days


@router.post("/CopyToAllDays")
def copy_to_all_days(data: CopyToAllDaysRequest, request: Request):
    try:
        validate_month_format(data.target_month)

        if not data.slots:
            raise HTTPException(status_code=400, detail="กรุณาระบุช่วงเวลาอย่างน้อย 1 ช่วง")

        if check_overlap([{"start": s.start, "end": s.end} for s in data.slots]):
            raise HTTPException(status_code=400, detail="ช่วงเวลาที่ระบุซ้อนกัน")

        _raise_if_too_many_slots(data.target_month, len({(s.start, s.end) for s in data.slots}))

        payload          = get_current_advisor(request)
        today            = get_today_utc7()
        year, month_num  = map(int, data.target_month.split("-"))
        _, days_in_month = calendar.monthrange(year, month_num)

        col    = get_db_collection()
        result = {}

        def attempt() -> bool:
            existing      = col.find_one(advisor_filter(payload))
            current_dates = existing.get("dates", {}) if existing else {}

            to_write, skipped_days, overwrite_days = _plan_copy_days(
                data.slots, year, month_num, days_in_month, today, current_dates
            )
            if not to_write:
                raise HTTPException(
                    status_code=400,
                    detail="ไม่มีวันที่สามารถ copy ได้ (ทุกวันในเดือนนี้ผ่านมาแล้ว หรือมีการจองอยู่ทั้งหมด)"
                )

            result.update(to_write=to_write, skipped_days=skipped_days, overwrite_days=overwrite_days)
            return _write_new_days(col, payload, existing, current_dates, to_write)

        _retry_write(attempt)
        to_write, skipped_days, overwrite_days = result["to_write"], result["skipped_days"], result["overwrite_days"]

        copied_days = len(to_write)
        return {
            "message"     : f"Copy ช่วงเวลาไปยัง {copied_days} วัน ในเดือน {data.target_month} สำเร็จ",
            "copied_days" : copied_days,
            "skipped_days": skipped_days,
            "warning"     : f"แทนที่ช่วงเวลาเดิมใน {len(overwrite_days)} วัน: {', '.join(overwrite_days)}"
                            if overwrite_days else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(">>> UNEXPECTED ERROR in CopyToAllDays: %s", e)
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")


# ─────────────────────────────────────────
# PUT /UpdateTimeSlots/{date}
# ─────────────────────────────────────────
@router.put("/UpdateTimeSlots/{date}")
def update_time_slot(date: str, data: UpdateSlotRequest, request: Request):
    try:
        # ใช้ validate_date_format_delete_update (ไม่ใช่ _write) เพราะ slot เดิมอาจอยู่ในวันที่
        # ผ่านมาแล้ว (เช่น 2026-06-06) และยังต้องแก้ไขได้
        validate_date_format_delete_update(date)

        payload = get_current_advisor(request)
        col     = get_db_collection()

        def attempt() -> bool:
            _, old_slots = _load_day_slots(col, payload, date)
            target = _find_unlocked_target(old_slots, date, data.old_start, data.old_end, "แก้ไข")

            # ─── นัดหมายแล้ว (มีนักศึกษาจอง) ห้ามแก้ไขช่วงเวลาก่อนถึงวันให้คำปรึกษา ───
            # ป้องกันได้ตรงกว่าเช็คแบบอิงวันที่ปฏิทิน (เช่น "ห้ามแก้ไขเฉพาะวันพรุ่งนี้")
            # เพราะครอบคลุมทุกนัดหมายไม่ว่าจะอยู่ห่างจากวันนี้กี่วัน และกันอาจารย์กดแก้ไข
            # นัดหมายที่มีอยู่แล้วโดยไม่ตั้งใจไปในตัว
            time_changed = data.new_start != data.old_start or data.new_end != data.old_end
            if target.get("booked", 0) > 0 and time_changed:
                raise HTTPException(
                    status_code=400,
                    detail=f"ไม่สามารถเปลี่ยนเวลา {data.old_start}-{data.old_end} ได้ เพราะมีนักศึกษาจองแล้ว"
                )

            other_slots = [s for s in old_slots if not is_slot(s, data.old_start, data.old_end)]
            if check_overlap(other_slots + [{"start": data.new_start, "end": data.new_end}]):
                raise HTTPException(status_code=400, detail="ช่วงเวลาใหม่ซ้อนกับช่วงเวลาอื่น")

            updated_slots = [
                preserving_slot(data.new_start, data.new_end, target) if is_slot(s, data.old_start, data.old_end) else s
                for s in old_slots
            ]
            return _write_if_unchanged(
                col, payload, {date: old_slots},
                {"$set": {f"dates.{date}": updated_slots, "updatedAt": datetime.now(timezone.utc)}},
            )

        _retry_write(attempt)

        return {"message": f"อัปเดตช่วงเวลา {data.old_start}-{data.old_end} → {data.new_start}-{data.new_end} สำเร็จ"}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")


# ─────────────────────────────────────────
# DELETE /DeleteTimeSlots/{date}
# ─────────────────────────────────────────
def _delete_last_slot_of_day(col, payload: dict, date: str, existing: dict, old_slots: list) -> bool:
    """ลบวันนั้นออกทั้งวัน (slot สุดท้ายถูกลบ) และตัดเดือนออกจาก months ถ้าไม่เหลือวันอื่นในเดือนนั้น

    คืน False ถ้าวันนั้นถูกแก้ตัดหน้า (ไม่ได้ลบ ให้ผู้เรียกอ่านใหม่แล้วลองซ้ำ)
    """
    month_of_date   = date[:7]
    existing_months = set(existing.get("months", []))

    full_doc        = col.find_one(advisor_filter(payload), {"_id": 0, "dates": 1})
    remaining_dates = full_doc.get("dates", {}) if full_doc else {}
    remaining_dates.pop(date, None)

    if not any(d.startswith(month_of_date) for d in remaining_dates):
        existing_months.discard(month_of_date)

    return _write_if_unchanged(
        col, payload, {date: old_slots},
        {
            "$unset": {f"dates.{date}": "", f"max_booking_config.{date}": ""},
            "$set"  : {"months": sorted(existing_months), "updatedAt": datetime.now(timezone.utc)},
        },
    )


@router.delete("/DeleteTimeSlots/{date}")
def delete_time_slot(date: str, data: DeleteSlotRequest, request: Request):
    try:
        # ใช้ validate_date_format_delete_update (ไม่ใช่ _write) เพราะ slot เดิมอาจอยู่ในวันที่
        # ผ่านมาแล้ว (เช่น 2026-06-06) และยังต้องลบได้
        validate_date_format_delete_update(date)

        payload = get_current_advisor(request)
        col     = get_db_collection()

        removed_whole_day = False

        def attempt() -> bool:
            nonlocal removed_whole_day
            existing, old_slots = _load_day_slots(col, payload, date, extra_fields={"months": 1})
            target = _find_unlocked_target(old_slots, date, data.start, data.end, "ลบ")

            # ─── นัดหมายแล้ว (มีนักศึกษาจอง) ห้ามลบก่อนถึงวันให้คำปรึกษา ไม่ว่าจะอยู่ห่างจาก
            # วันนี้กี่วันก็ตาม (แทนที่เช็คแบบอิงวันที่ปฏิทินเดิม ที่ป้องกันเฉพาะ "วันพรุ่งนี้") ───
            if target.get("booked", 0) > 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"ไม่สามารถลบช่วงเวลา {data.start}-{data.end} ได้ เพราะมีนักศึกษาจองแล้ว"
                )

            updated_slots = [s for s in old_slots if not is_slot(s, data.start, data.end)]
            removed_whole_day = not updated_slots
            if removed_whole_day:
                return _delete_last_slot_of_day(col, payload, date, existing, old_slots)
            return _write_if_unchanged(
                col, payload, {date: old_slots},
                {"$set": {f"dates.{date}": updated_slots, "updatedAt": datetime.now(timezone.utc)}},
            )

        _retry_write(attempt)

        if removed_whole_day:
            return {"message": f"ลบช่วงเวลาสุดท้ายของวันที่ {date} สำเร็จ"}
        return {"message": f"ลบช่วงเวลา {data.start}-{data.end} ของวันที่ {date} สำเร็จ"}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")

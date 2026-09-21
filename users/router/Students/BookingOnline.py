import logging
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form, Depends, BackgroundTasks
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id
from datetime import datetime, timezone
import requests as req
from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError
from common.booking_status import ACTIVE_STATUSES
from common.slot_service import (
    cutoff_datetime,
    get_now_utc7,
    has_bookable_slot,
    recalculate_slot_booked,
    slot_is_taken,
    unavailable_advisor_ids,
    validate_date_or_400,
)
from common.attachments import Attachment, content_matches_type, delete_attachments, store_attachment
from common.notify import CHATBOT_INTERNAL_HEADERS, CHATBOT_URL

logger = logging.getLogger(__name__)

router = APIRouter()

load_dotenv(override=True)
chatbot_uri = CHATBOT_URL

# ─── Constants ────────────────────────────────────────────────────────────────
ALLOWED_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
]
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# ─── Form model ───────────────────────────────────────────────────────────────
class BookingForm:
    def __init__(
        self,
        advisor_id     : str = Form(...),
        advisor_name   : str = Form(...),
        date           : str = Form(...),
        time           : str = Form(...),
        research_topic : str = Form(...),
        research_detail: str = Form(...),
    ):
        self.advisor_id      = advisor_id
        self.advisor_name    = advisor_name
        self.date            = date
        self.time            = time
        self.research_topic  = research_topic
        self.research_detail = research_detail


# ─── DB helpers ───────────────────────────────────────────────────────────────
def get_db():
    return Connect_MongoDB()["BORC"]




# ─── Slot helpers ─────────────────────────────────────────────────────────────
def parse_time_range(time_str: str) -> tuple[str, str]:
    """แยก '09:00-10:00' → ('09:00', '10:00')"""
    parts = time_str.split("-")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="รูปแบบเวลาไม่ถูกต้อง (HH:MM-HH:MM)")
    return parts[0].strip(), parts[1].strip()


def find_slot_doc_for_date(db, advisor_id: str, date: str):
    """ค้นหา document slot ของอาจารย์ในวันที่ระบุ"""
    return db["ManageTimeSlots"].find_one({
        "advisorId"     : advisor_id,
        f"dates.{date}" : {"$exists": True},
    })


def _slot_view(s: dict, date: str, today: str, now: datetime) -> dict:
    """รูปแบบ slot ที่ส่งให้หน้าจอง พร้อมสถานะปิด/เต็ม/เลยเวลา

    (เวลาเริ่มที่ผิดรูปแบบของวันนี้ ถือว่ายังไม่เลยเวลา: พฤติกรรมเดิมของหน้าเลือกเวลา)
    """
    is_past = False
    if date == today:
        cutoff = cutoff_datetime(date, s["start"])
        is_past = cutoff is not None and now >= cutoff
    return {
        "start"    : s["start"],
        "end"      : s["end"],
        "label"    : s.get("label", ""),
        "is_closed": slot_is_taken(s) or is_past,
        "is_past"  : is_past,
        "is_booked": s.get("isLocked", False) or s.get("booked", 0) >= s.get("max_booking", 1),
    }


# ─── GET /AvailableAdvisors ───────────────────────────────────────────────────
@router.get("/AvailableAdvisors")
def get_available_advisors(request: Request):
    """ดึงรายชื่ออาจารย์ที่มี slot ว่างตั้งแต่วันนี้เป็นต้นไป"""
    try:
        verify_user_token(request)
        db       = get_db()
        now_utc7 = get_now_utc7()
        today    = now_utc7.strftime("%Y-%m-%d")

        all_docs = list(db["ManageTimeSlots"].find(
            {},
            {"_id": 0, "advisor_name": 1, "advisorId": 1, "dates": 1}
        ))
        blocked = unavailable_advisor_ids(db, (d.get("advisorId", "") for d in all_docs))

        advisors: dict = {}
        for doc in all_docs:
            advisor_id = doc.get("advisorId", "")
            name       = doc.get("advisor_name", "")
            if not advisor_id or not name or advisor_id in blocked:
                continue

            entry = advisors.setdefault(advisor_id, {"name": name, "has_available": False})
            if entry["has_available"]:
                continue
            entry["has_available"] = any(
                isinstance(slots, list) and date >= today and has_bookable_slot(slots, date, today, now_utc7)
                for date, slots in doc.get("dates", {}).items()
            )

        return {"advisors": [
            {"advisor_id": aid, "advisor_name": info["name"]}
            for aid, info in advisors.items()
            if info["has_available"]
        ]}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /AvailableSlots/{advisor_id} ────────────────────────────────────────
@router.get("/AvailableSlots/{advisor_id}")
def get_available_slots(advisor_id: str, request: Request):
    """ดึง slot ทั้งหมดของอาจารย์ พร้อมสถานะว่าง/ถูกจอง"""
    try:
        verify_user_token(request)
        db       = get_db()
        now_utc7 = get_now_utc7()
        today    = now_utc7.strftime("%Y-%m-%d")

        docs = list(db["ManageTimeSlots"].find(
            {"advisorId": advisor_id},
            {"_id": 0, "dates": 1, "advisorId": 1}
        ))
        if not docs:
            return {"dates": {}}

        merged_dates = {}
        for doc in docs:
            for date, slots in doc.get("dates", {}).items():
                if not isinstance(slots, list) or date < today:
                    continue
                merged_dates[date] = [_slot_view(s, date, today, now_utc7) for s in slots]

        return {"advisor_id": advisor_id, "dates": merged_dates}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Background notify helper ─────────────────────────────────────────────────
def _notify_advisor_background(chatbot_url: str, payload: dict):
    """ส่งแจ้งเตือนอาจารย์ใน background thread ไม่บล็อก response"""
    try:
        req.post(chatbot_url, json=payload, timeout=8, headers=CHATBOT_INTERNAL_HEADERS)
    except Exception as e:
        logger.warning(f"[WARN] แจ้งเตือน Advisor ล้มเหลว: {e}")


# ─── POST /BookingOnline helpers ──────────────────────────────────────────────
def _validate_attachment(file: UploadFile | None) -> Attachment | None:
    """ตรวจไฟล์แนบ (PDF/DOC/DOCX ≤ 10MB และเนื้อไฟล์ตรงกับชนิด) คืนไฟล์ หรือ None ถ้าไม่มีไฟล์"""
    if not (file and file.filename):
        return None
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="อนุญาตเฉพาะไฟล์ PDF หรือ DOCX เท่านั้น")
    # def ธรรมดา (รันใน threadpool) จึงอ่านผ่าน file.file แบบ sync ได้
    contents = file.file.read()
    if len(contents) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"ขนาดไฟล์ต้องไม่เกิน 10 MB (ไฟล์นี้ {len(contents)/1024/1024:.2f} MB)"
        )
    if not content_matches_type(file.content_type, contents):
        raise HTTPException(status_code=400, detail="เนื้อหาไฟล์ไม่ตรงกับชนิดไฟล์ที่ระบุ")
    return Attachment(filename=file.filename[:255], content_type=file.content_type, contents=contents)


def _store_attachment(db, attachment: Attachment | None) -> str | None:
    """เก็บไฟล์แนบลง GridFS คืน id; ล้มเหลว = 502 (ยังไม่ได้ล็อก slot จึงไม่มีอะไรต้องคืน)"""
    if attachment is None:
        return None
    try:
        return store_attachment(db, attachment)
    except Exception:
        logger.exception("บันทึกไฟล์แนบไม่สำเร็จ")
        raise HTTPException(status_code=502, detail="บันทึกไฟล์แนบไม่สำเร็จ กรุณาลองใหม่อีกครั้ง")


def _ensure_no_active_booking(db, user_id: str) -> None:
    """raise 400 ถ้าคิวล่าสุดของผู้ใช้ยังไม่เสร็จสิ้น (นักศึกษามีคิวที่ใช้งานอยู่ได้ทีละ 1 คิว)"""
    latest_booking = db["BookingOnline"].find_one(
        {"UserId": user_id},
        sort=[("CreatedAt", -1)]
    )
    if latest_booking and latest_booking.get("Status") in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"คุณมีการจองที่ยังไม่เสร็จสิ้น "
                f"(วันที่ {latest_booking['Date']} เวลา {latest_booking['Time']})"
            ),
        )


def _find_bookable_slot(db, data) -> tuple[dict, str, str, str]:
    """ตรวจวันที่/เวลา/slot ที่เลือก คืน (slot_doc, start, end, advisor_name); raise ถ้าจองไม่ได้"""
    validate_date_or_400(data.date)
    min_date = get_now_utc7().strftime("%Y-%m-%d")
    if data.date < min_date:
        raise HTTPException(
            status_code=400,
            detail=f"กรุณาเลือกวันนี้ ({min_date}) หรือหลังจากนั้น"
        )

    if data.advisor_id in unavailable_advisor_ids(db, [data.advisor_id]):
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลอาจารย์")

    slot_doc = find_slot_doc_for_date(db, data.advisor_id, data.date)
    if not slot_doc:
        raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาของอาจารย์ในวันที่เลือก")

    start, end = parse_time_range(data.time)

    cutoff = cutoff_datetime(data.date, start)  # None = เวลาเริ่มผิดรูปแบบ → ข้ามการเช็ก (พฤติกรรมเดิม)
    if cutoff is not None and get_now_utc7() >= cutoff:
        raise HTTPException(status_code=400, detail="ไม่สามารถจองได้ เนื่องจากต้องจองล่วงหน้าอย่างน้อย 1 ชั่วโมง หรือช่วงเวลานี้ผ่านไปแล้ว")

    slot_list   = slot_doc.get("dates", {}).get(data.date, [])
    target_slot = next(
        (s for s in slot_list if s["start"] == start and s["end"] == end), None
    )
    if not target_slot:
        raise HTTPException(status_code=404, detail="ไม่พบช่วงเวลาที่เลือก")

    if slot_is_taken(target_slot):
        raise HTTPException(status_code=400, detail="ช่วงเวลานี้ถูกจองแล้ว กรุณาเลือกช่วงเวลาอื่น")

    # ใช้ชื่อจากตารางของอาจารย์เสมอ ไม่เชื่อชื่อที่ส่งมาจาก browser
    advisor_name = slot_doc.get("advisor_name", "").strip()
    if not advisor_name:
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลชื่ออาจารย์")

    return slot_doc, start, end, advisor_name


def _lock_slot(db, slot_doc: dict, date: str, start: str, end: str) -> None:
    """Atomic lock slot (ป้องกัน race condition / double booking); raise ถ้ามีคนจองตัดหน้า"""
    inc_result = db["ManageTimeSlots"].update_one(
        {"_id": slot_doc["_id"]},
        {
            "$inc": {f"dates.{date}.$[slot].booked": 1},
            "$set": {
                f"dates.{date}.$[slot].isLocked" : True,
                f"dates.{date}.$[slot].is_closed": True,
            },
        },
        array_filters=[{
            "slot.start"   : start,
            "slot.end"     : end,
            "slot.isLocked": False,
            "slot.is_closed": {"$ne": True},
            "slot.booked"  : 0,
        }]
    )
    if inc_result.modified_count == 0:
        raise HTTPException(status_code=400, detail="ช่วงเวลานี้ถูกจองแล้ว กรุณาเลือกช่วงเวลาอื่น")


def _get_student_name(db, user_id: str) -> str:
    profile = db["UserProfile"].find_one(
        {"userId": user_id},
        {"_id": 0, "Prefix": 1, "Firstname": 1, "Lastname": 1}
    )
    if not profile:
        return ""
    return (
        f"{profile.get('Prefix', '')}"
        f"{profile.get('Firstname', '')} "
        f"{profile.get('Lastname', '')}".strip()
    )


def _save_booking(db, booking_payload: dict) -> None:
    """คิวใหม่เป็นเอกสารใหม่เสมอ (คิวเก่าที่ Cancelled/Completed เก็บไว้เป็นประวัติ)

    เดิมมีกิ่ง "update เอกสารเดิมถ้า Approved/Rescheduled" แต่ไปไม่ถึงเลย: _ensure_no_active_booking
    ปฏิเสธคิวที่ยัง active (รวมสองสถานะนี้) ตั้งแต่ก่อนถึงขั้นนี้
    """
    db["BookingOnline"].insert_one(booking_payload)


# ─── POST /BookingOnline ──────────────────────────────────────────────────────
@router.post("/BookingOnline")
def create_booking(
    request         : Request,
    background_tasks: BackgroundTasks,
    data            : BookingForm = Depends(),
    file            : UploadFile  = File(None),
):
    """
    จอง slot กับอาจารย์ที่ปรึกษา
    - ตรวจสอบไฟล์แนบ (PDF/DOCX ≤ 10MB)
    - บล็อกถ้าผู้ใช้มีการจองที่ยังไม่เสร็จ (Pending/InProgress)
    - ล็อก slot ด้วย atomic update เพื่อป้องกัน double booking
    """
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)

        attachment = _validate_attachment(file)

        db = get_db()
        # ไม่เรียก auto_update_status ตรงนี้: background worker (main.booking_status_worker) ทำทุก 30 วินาทีอยู่แล้ว
        # เดิมคำขอจองต้องรอสแกนคิวทั้งระบบ + ยิงแจ้งเตือน LINE (timeout 5 วินาทีต่อรายการ) ก่อนได้ตอบ

        _ensure_no_active_booking(db, user_id)
        slot_doc, start, end, advisor_name = _find_bookable_slot(db, data)

        # เก็บไฟล์ก่อนล็อก slot: การเขียนไฟล์ 10MB ช้ากว่าปกติ ไม่ควรถือ slot ที่ล็อกไว้ระหว่างรอ
        file_id = _store_attachment(db, attachment)
        try:
            _lock_slot(db, slot_doc, data.date, start, end)

            # หลังล็อก slot แล้ว ถ้าบันทึกคิวไม่สำเร็จต้องคืน slot ไม่งั้นจะล็อกค้างโดยไม่มีคิว
            try:
                student_name = _get_student_name(db, user_id)

                now = datetime.now(timezone.utc)
                _save_booking(
                    db,
                    {
                        "UserId"                : user_id,
                        "StudentName"           : student_name,
                        "AdvisorId"             : data.advisor_id,
                        "Advisor_Name"          : advisor_name,
                        "Date"                  : data.date,
                        "Time"                  : data.time,
                        "ResearchTopic"         : data.research_topic,
                        "ResearchDetail"        : data.research_detail,
                        "FilePath"              : attachment.filename if attachment else None,
                        "FileName"              : attachment.filename if attachment else None,
                        "FileContentType"       : attachment.content_type if attachment else None,
                        "FileSize"              : len(attachment.contents) if attachment else None,
                        "FileId"                : file_id,
                        "Status"                : "Pending",
                        "RescheduledOnce"       : False,
                        "AdvisorRescheduledOnce": False,
                        "CreatedAt"             : now,
                        "UpdatedAt"             : now,
                    },
                )
            except Exception as exc:
                recalculate_slot_booked(db, data.advisor_id, data.date, start, end)
                if isinstance(exc, DuplicateKeyError):
                    # มีคำขอจองอีกอันของผู้ใช้เดียวกันสำเร็จตัดหน้า (unique index one_active_booking_per_user)
                    raise HTTPException(status_code=400, detail="คุณมีการจองที่ยังไม่เสร็จสิ้นอยู่แล้ว")
                raise
        except Exception:
            # จองไม่สำเร็จ (slot ถูกตัดหน้า/บันทึกล้มเหลว): ไฟล์ที่เก็บไปแล้วไม่มีคิวอ้างอิง ลบทิ้ง
            delete_attachments(db, [file_id])
            raise

        # แจ้งเตือนอาจารย์มีนักศึกษามาจอง (ทำงานใน background ไม่บล็อก response)
        background_tasks.add_task(
            _notify_advisor_background,
            f"{chatbot_uri}/NotifyQueueAdivsor/BookingStudent",
            {
                "AdvisorId"    : data.advisor_id,
                "StudentName"  : student_name,
                "ResearchTopic": data.research_topic,
                "Date"         : data.date,
                "Time"         : data.time,
                "Status"       : "Pending"
            },
        )

        return {"message": "เสร็จสิ้นการจองคิวให้คำปรึกษา"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── GET /BookingStatus ───────────────────────────────────────────────────────
@router.get("/BookingStatus")
def get_booking_status(request: Request):
    """
    ดูสถานะการจองล่าสุดของผู้ใช้
    - ถ้า active → คืนข้อมูล booking + can_book: False
    - ถ้า Completed หรือ Cancelled → should_reset: True เพื่อให้ frontend reset UI
    - ถ้าไม่มี   → can_book: True
    """
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        db      = get_db()

        # ใช้ CreatedAt -1 เพื่อให้สอดคล้องกับ /BookingOnline POST ที่เช็คคิวล่าสุด
        booking = db["BookingOnline"].find_one(
            {"UserId": user_id},
            sort=[("CreatedAt", -1)]
        )
        if not booking:
            return {"status": None, "should_reset": False, "can_book": True}

        status    = booking.get("Status", "")
        is_active = status in ACTIVE_STATUSES

        return {
            "status"      : status,
            "should_reset": status in {"Completed", "Cancelled"},
            "can_book"    : not is_active,
            "booking"     : {
                "AdvisorId"    : booking.get("AdvisorId", ""),
                "Advisor_Name" : booking.get("Advisor_Name", ""),
                "Date"         : booking.get("Date", ""),
                "Time"         : booking.get("Time", ""),
                "ResearchTopic": booking.get("ResearchTopic", ""),
                "Status"       : status,
            } if is_active else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")

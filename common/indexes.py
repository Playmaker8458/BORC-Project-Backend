"""สร้าง index ตอน startup (เรียกจาก main.lifespan): unique index ที่ระบบพึ่งพาความถูกต้อง และ index ของ query ที่ยิงบ่อย

- UserProfile.userId  : กันสมัครซ้ำพร้อมกัน (เดิมเช็ก find แล้ว insert จึงซ้ำได้)
- ManageTimeSlots.advisorId : หนึ่งอาจารย์หนึ่งเอกสาร (โค้ดฝั่ง slot ทั้งหมดถือว่า find_one ได้เอกสารเดียว
  และอาจารย์ใหม่ที่กดบันทึกสองครั้งพร้อมกันเคยสร้างเอกสารซ้ำได้)

ถ้าข้อมูลเดิมมีค่าซ้ำอยู่แล้ว unique index จะสร้างไม่ได้: log error แล้วสร้าง index ธรรมดาแทน (ไม่ล้ม startup
และคิวรียังเร็วเหมือนเดิม) — ต้องแก้ข้อมูลซ้ำแล้วรีสตาร์ตให้ unique มีผล
"""

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from common.booking_status import ACTIVE_STATUSES

logger = logging.getLogger(__name__)


def ensure_unique_index(col, key: str, name: str) -> bool:
    """สร้าง unique index บน `key`; คืน True ถ้าเป็น unique จริง, False ถ้าทำไม่ได้ (ตกไปใช้ index ธรรมดา)"""
    keys = [(key, ASCENDING)]
    dropped_plain = []
    try:
        # index ธรรมดาบน key เดียวกัน (คนละชื่อ) ชนกับ unique index ที่จะสร้าง → ต้องลบก่อน
        for existing_name, info in col.index_information().items():
            if existing_name != name and list(info.get("key", [])) == keys and not info.get("unique"):
                col.drop_index(existing_name)
                dropped_plain.append(existing_name)
        col.create_index(keys, unique=True, name=name)
        return True
    except Exception:
        logger.exception("สร้าง unique index %s.%s ไม่สำเร็จ (ตรวจข้อมูลซ้ำ) — ใช้ index ธรรมดาแทน", col.name, key)
        try:
            col.create_index(keys)
        except Exception:
            logger.exception("สร้าง index ธรรมดา %s.%s ไม่สำเร็จ", col.name, key)
        return False


def ensure_unique_indexes(db) -> dict:
    """คืนผลรายชื่อ index → สร้างเป็น unique สำเร็จหรือไม่ (ไว้ log/ทดสอบ)"""
    result = {
        "UserProfile.userId": ensure_unique_index(db["UserProfile"], "userId", "userId_unique"),
        "ManageTimeSlots.advisorId": ensure_unique_index(db["ManageTimeSlots"], "advisorId", "advisorId_unique"),
    }
    try:
        db["ManageTimeSlots"].create_index([("advisor_name", ASCENDING)], name="advisor_name_idx")
    except Exception:
        logger.exception("สร้าง index ManageTimeSlots.advisor_name ไม่สำเร็จ")
    return result


def ensure_booking_indexes(db):
    """สร้าง index ครั้งเดียวตอน startup หรือเรียกจาก lifespan"""
    col = db["BookingOnline"]
    col.create_index([("UserId",    ASCENDING), ("Status", ASCENDING)])
    col.create_index([("Status",    ASCENDING), ("Date",   ASCENDING)])
    col.create_index([("AdvisorId", ASCENDING), ("Date",   ASCENDING), ("Status", ASCENDING)])
    
    col.create_index([("UserId", ASCENDING), ("CreatedAt", DESCENDING)])

    # นักศึกษามีคิวที่ยังใช้งานอยู่ได้ไม่เกิน 1 รายการ: กันกดจองซ้ำพร้อมกัน (คนละ slot) ที่การเช็กใน
    # โค้ดกันไม่ได้ ถ้าข้อมูลเดิมมีคิว active ซ้ำอยู่ index จะสร้างไม่ได้ — log แล้วข้าม ไม่ล้ม startup
    try:
        col.create_index(
            [("UserId", ASCENDING)],
            unique=True,
            name="one_active_booking_per_user",
            partialFilterExpression={"Status": {"$in": ACTIVE_STATUSES}},
        )
    except Exception:
        logger.exception("สร้าง unique index one_active_booking_per_user ไม่สำเร็จ (ตรวจคิว active ซ้ำของผู้ใช้เดียวกัน)")

    # index สำหรับ query ที่ยิงบ่อย: verify_user_token ค้น UserProfile ด้วย userId ทุก request
    # (ก่อนหน้านี้ไม่มี index จึงเป็น collection scan) และ history ที่ค้นด้วย id ของผู้ใช้/คิว
    # UserProfile.userId (unique) สร้างใน common/indexes.py ตอน startup
    db["RescheduleHistory"].create_index([("bookingId", ASCENDING), ("rescheduledById", ASCENDING)])
    db["RescheduleHistory"].create_index([("studentId", ASCENDING)])
    # หน้าจัดการคิวของอาจารย์ค้นประวัติเลื่อนคิวด้วย rescheduledById + rescheduledByRole (รันพร้อมกับการดึงคิว)
    db["RescheduleHistory"].create_index([("rescheduledById", ASCENDING), ("rescheduledByRole", ASCENDING)])
    db["ApprovedHistory"].create_index([("UserId", ASCENDING)])
    db["CancelBookingHistory"].create_index([("cancelledById", ASCENDING)])
    db["QueueManagementHistory"].create_index([("userId", ASCENDING), ("status", ASCENDING)])
    # แจ้งเตือน: ดึงรายการล่าสุดของผู้ใช้เรียงตามเวลา (GET /Notifications/List)
    db["QueueManagementHistory"].create_index([("userId", ASCENDING), ("createdAt", DESCENDING)])
    try:
        db["NotificationReadState"].create_index([("userId", ASCENDING)], unique=True, name="userId_unique")
    except Exception:
        logger.exception("สร้าง unique index NotificationReadState.userId ไม่สำเร็จ")
    # AdvisorStats นับด้วย AdvisorId / advisorName
    db["ApprovedHistory"].create_index([("AdvisorId", ASCENDING), ("Status", ASCENDING)])
    # advisorName ยังไว้ใช้นับประวัติเก่าที่ยังไม่มี advisorId
    db["RescheduleHistory"].create_index([("advisorName", ASCENDING)])
    db["CancelBookingHistory"].create_index([("advisorName", ASCENDING)])
    db["RescheduleHistory"].create_index([("advisorId", ASCENDING)])
    db["CancelBookingHistory"].create_index([("advisorId", ASCENDING)])

    lock_col = db["_AutoUpdateLock"]
    
    # ─── แก้ไขตรงส่วนนี้ ───
    try:
        lock_col.create_index([("key", ASCENDING)], unique=True, name="auto_update_key_unique")
    except DuplicateKeyError:
        # หากมีข้อมูล key: "auto_update" ซ้ำกันอยู่ ให้ลบข้อมูลใน _AutoUpdateLock ทิ้ง
        lock_col.delete_many({}) 
        # แล้วลองสร้าง Index ใหม่อีกครั้ง
        lock_col.create_index([("key", ASCENDING)], unique=True, name="auto_update_key_unique")
        
    lock_col.update_one(
        {"key": "auto_update"},
        {"$setOnInsert": {"lastRun": datetime(1970, 1, 1, tzinfo=timezone.utc)}},
        upsert=True,
    )

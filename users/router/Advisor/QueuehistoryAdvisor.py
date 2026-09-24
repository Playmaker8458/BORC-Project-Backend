import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import get_current_advisor
from common.parallel import run_parallel
from datetime import datetime

router = APIRouter()

# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def get_db():
    try:
        return Connect_MongoDB()["BORC"]
    except Exception:
        logger.exception("ไม่สามารถเชื่อมต่อฐานข้อมูลได้")
        raise HTTPException(status_code=500, detail="ไม่สามารถเชื่อมต่อฐานข้อมูลได้")


# API All ใช้สำหรับรวมประวัติการจัดการคิวของอาจารย์ทั้งหมด (อนุมัติ/เลื่อน/ยกเลิก/เสร็จสิ้น)
# มาจากตารางที่เก็บข้อมูลจริงของแต่ละเหตุการณ์ (ApprovedHistory / RescheduleHistory /
# CancelBookingHistory) แทนที่จะพึ่ง QueueManagementHistory อย่างเดียว เพราะตารางนั้นเพิ่งถูก
# เติม entry ให้ครบทุก action ภายหลัง ข้อมูลเก่าก่อนหน้าจึงไม่มีบันทึกไว้ (เหมือนที่แก้ไปแล้วฝั่ง
# นักศึกษาใน QueuehistoryStudent.py — เดิมไฟล์นี้ query แค่ QueueManagementHistory ที่เดียว ทำให้
# อาจารย์เห็นประวัติไม่ครบ โดยเฉพาะเหตุการณ์เก่าก่อนที่ตารางนั้นจะถูกเติมข้อมูลให้ครบ)
# (ยกเว้นสถานะ "Completed" ที่มีบันทึกอยู่ที่ QueueManagementHistory ที่เดียว)
@router.get("/All")
def get_all_queue_history(request: Request):
    try:
        payload    = get_current_advisor(request)
        advisor_id = payload["user_id"]
        db         = get_db()

        history: list[dict] = []

        # 4 collection อิสระต่อกัน -> query พร้อมกัน (รอ DB รอบเดียวแทน 4 รอบ)
        # แล้วต่อผลตามลำดับเดิม (Approved, Reschedule, Cancel, Completed) เพื่อให้ลำดับตอน
        # createdAt เท่ากันเหมือนเดิม
        approved_docs, reschedule_docs, cancel_docs, completed_docs = run_parallel(
            lambda: list(db["ApprovedHistory"].find({"AdvisorId": advisor_id})),
            lambda: list(db["RescheduleHistory"].find({"advisorId": advisor_id})),
            # advisorId ถูกบันทึกไว้เสมอไม่ว่าใครจะเป็นคนยกเลิก (นักศึกษา/อาจารย์/ระบบ/แอดมิน)
            # จึงไม่ต้องเผื่อ cancelledById="system" เหมือนฝั่งนักศึกษา (ที่ id ไม่ตรงกับผู้ใช้จริง)
            lambda: list(db["CancelBookingHistory"].find({"advisorId": advisor_id})),
            lambda: list(db["QueueManagementHistory"].find(
                {"userId": advisor_id, "status": "Completed"},
                {"_id": 0},
            )),
        )

        for doc in approved_docs:
            history.append({
                "UserName" : doc.get("StudentName", ""),
                "role"     : "Advisor",
                "status"   : doc.get("Status", "Approved"),
                "Reason"   : None,
                "createdAt": doc.get("CreatedAt"),
                "updatedAt": doc.get("UpdatedAt"),
            })

        for doc in reschedule_docs:
            history.append({
                "UserName" : doc.get("studentName", ""),
                "role"     : doc.get("rescheduledByRole", ""),
                "status"   : doc.get("status", "Rescheduled"),
                "Reason"   : doc.get("rescheduledReason"),
                "createdAt": doc.get("createdAt"),
                "updatedAt": doc.get("updatedAt"),
            })

        for doc in cancel_docs:
            history.append({
                "UserName" : doc.get("studentName", ""),
                "role"     : doc.get("cancelledByRole", ""),
                "status"   : doc.get("status", "Cancelled"),
                "Reason"   : doc.get("cancelReason"),
                "createdAt": doc.get("createdAt"),
                "updatedAt": doc.get("updatedAt"),
            })

        for doc in completed_docs:
            history.append({
                "UserName" : doc.get("UserName", ""),
                "role"     : doc.get("role", ""),
                "status"   : doc.get("status", "Completed"),
                "Reason"   : doc.get("Reason"),
                "createdAt": doc.get("createdAt"),
                "updatedAt": doc.get("updatedAt"),
            })

        def _sort_key(item):
            value = item.get("createdAt")
            if not isinstance(value, datetime):
                return datetime(1970, 1, 1)
            return value.replace(tzinfo=None)

        history.sort(key=_sort_key, reverse=True)

        return {"data": history}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")

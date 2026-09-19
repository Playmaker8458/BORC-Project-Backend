import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import get_current_student
from datetime import datetime, timezone, timedelta

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


# API All ใช้สำหรับรวมประวัติการจัดการคิวของนักศึกษาทั้งหมด (อนุมัติ/เลื่อน/ยกเลิก/เสร็จสิ้น)
# มาจากตารางที่เก็บข้อมูลจริงของแต่ละเหตุการณ์ (ApprovedHistory / RescheduleHistory /
# CancelBookingHistory) แทนที่จะพึ่ง QueueManagementHistory อย่างเดียว เพราะตารางนั้นเพิ่งถูก
# เติม entry ฝั่งนักศึกษาให้ครบทุก action ภายหลัง ข้อมูลเก่าก่อนหน้าจึงไม่มีฝั่งนักศึกษาบันทึกไว้
# (ยกเว้นสถานะ "Completed" ที่มีบันทึกอยู่ที่ QueueManagementHistory ที่เดียว)
@router.get("/History")
def get_all_queue_history(request: Request):
    try:
        payload = get_current_student(request)
        user_id = payload["user_id"]
        db      = get_db()

        history: list[dict] = []

        for doc in db["ApprovedHistory"].find({"UserId": user_id}):
            history.append({
                "UserName" : doc.get("AdvisorName", ""),
                "role"     : "Advisor",
                "status"   : doc.get("Status", "Approved"),
                "Reason"   : None,
                "createdAt": doc.get("CreatedAt"),
                "updatedAt": doc.get("UpdatedAt"),
            })

        for doc in db["RescheduleHistory"].find(
            {"$or": [{"rescheduledById": user_id}, {"studentId": user_id}]}
        ):
            history.append({
                "UserName" : doc.get("advisorName", ""),
                "role"     : doc.get("rescheduledByRole", ""),
                "status"   : doc.get("status", "Rescheduled"),
                "Reason"   : doc.get("rescheduledReason"),
                "createdAt": doc.get("createdAt"),
                "updatedAt": doc.get("updatedAt"),
            })

        for doc in db["CancelBookingHistory"].find({"cancelledById": user_id}):
            history.append({
                "UserName" : doc.get("advisorName", ""),
                "role"     : doc.get("cancelledByRole", ""),
                "status"   : doc.get("status", "Cancelled"),
                "Reason"   : doc.get("cancelReason"),
                "createdAt": doc.get("createdAt"),
                "updatedAt": doc.get("updatedAt"),
            })

        for doc in db["QueueManagementHistory"].find(
            {"userId": user_id, "status": "Completed"}, {"_id": 0}
        ):
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
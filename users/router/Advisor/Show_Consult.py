import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id

router = APIRouter()

def get_today_consultation_students(advisor_id: str) -> list:
    try:
        db = Connect_MongoDB()["BORC"]

        # ✅ ดึงทุก booking ที่ยังไม่เสร็จสิ้น ไม่ filter วันที่
        mydoc = db["BookingOnline"].find(
            {
                "AdvisorId": advisor_id,
                "Status"   : {"$in": ["Pending", "Approved", "Rescheduled"]},
            },
            {"_id": 0}
        )

        return list(mydoc)

    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในการดึงข้อมูล: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("/TodayQueue")
def get_today_queue(request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)

        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        queue = get_today_consultation_students(advisor_id)

        return {"consultation": queue, "total": len(queue)}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/AdvisorStats")
def Get_Advisor_Stats(request: Request):
    try:
        payload    = verify_user_token(request)
        advisor_id = get_user_id(payload)

        if not advisor_id:
            raise HTTPException(status_code=401, detail="ไม่พบ advisor_id ใน token")

        db = Connect_MongoDB()["BORC"]


        total_approved = db["ApprovedHistory"].count_documents({
            "AdvisorId": advisor_id,
            "Status"   : "Approved"
        })

        advisor_doc = db["UserProfile"].find_one(
            {"userId": advisor_id},
            {"_id": 0, "Prefix": 1, "Firstname": 1, "Lastname": 1}
        )
        advisor_name = ""
        if advisor_doc:
            advisor_name = (
                f"{advisor_doc.get('Prefix','')}"
                f"{advisor_doc.get('Firstname','')} "
                f"{advisor_doc.get('Lastname','')}".strip()
            )

        # ✅ นับการเลื่อนคิวทั้งหมดของอาจารย์คนนี้ ไม่ว่าใครเป็นคนกดเลื่อน (อาจารย์หรือนักศึกษา)
        #    เดิม filter เฉพาะ rescheduledById+rescheduledByRole="Advisor" ทำให้ไม่นับกรณี
        #    นักศึกษาเป็นคนเลื่อนคิวเอง — ทั้งสองฝั่งบันทึก advisorName ไว้เสมอ จึงใช้ field
        #    นี้จับคู่แทน (RescheduleHistory ไม่มี advisorId เก็บไว้)
        total_rescheduled = db["RescheduleHistory"].count_documents({
            "advisorName": advisor_name,
        })

        # ✅ นับการยกเลิกคิวทั้งหมดของอาจารย์คนนี้ ไม่ว่าใครเป็นคนกดยกเลิก
        #    เดิม filter เฉพาะ cancelledByRole="Advisor" ทำให้ไม่นับกรณีนักศึกษายกเลิกคิวเอง
        total_cancelled = db["CancelBookingHistory"].count_documents({
            "advisorName": advisor_name,
        })

        # ✅ นับจำนวนที่ปิดการให้คำปรึกษาแล้ว (ทั้งอาจารย์กดปิดเองและระบบปิดอัตโนมัติ)
        total_completed = db["QueueManagementHistory"].count_documents({
            "userId": advisor_id,
            "status": "Completed",
        })

        return {
            "stats": {
                "Approved"   : total_approved,
                "rescheduled": total_rescheduled,
                "cancelled"  : total_cancelled,
                "completed"  : total_completed,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
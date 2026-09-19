import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id
from common.parallel import run_parallel

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


        # ชื่ออาจารย์ใช้ค่าที่ verify_user_token ตรวจแล้ว (สูตรเดียวกับเดิม) ไม่ต้องค้น UserProfile ซ้ำ
        advisor_name = (
            f"{payload.get('Prefix', '')}"
            f"{payload.get('Firstname', '')} "
            f"{payload.get('Lastname', '')}".strip()
        )

        # 4 count อิสระต่อกัน -> query พร้อมกัน (รอ DB รอบเดียวแทน 5 รอบ)
        total_approved, total_rescheduled, total_cancelled, total_completed = run_parallel(
            lambda: db["ApprovedHistory"].count_documents({
                "AdvisorId": advisor_id,
                "Status"   : "Approved"
            }),
            # ✅ นับการเลื่อนคิวทั้งหมดของอาจารย์คนนี้ ไม่ว่าใครเป็นคนกดเลื่อน (อาจารย์หรือนักศึกษา)
            #    เดิม filter เฉพาะ rescheduledById+rescheduledByRole="Advisor" ทำให้ไม่นับกรณี
            #    นักศึกษาเป็นคนเลื่อนคิวเอง — ทั้งสองฝั่งบันทึก advisorName ไว้เสมอ จึงใช้ field
            #    นี้จับคู่แทน (RescheduleHistory ไม่มี advisorId เก็บไว้)
            lambda: db["RescheduleHistory"].count_documents({"advisorName": advisor_name}),
            # ✅ นับการยกเลิกคิวทั้งหมดของอาจารย์คนนี้ ไม่ว่าใครเป็นคนกดยกเลิก
            #    (เดิม filter เฉพาะ cancelledByRole="Advisor" ทำให้ไม่นับกรณีนักศึกษายกเลิกเอง)
            lambda: db["CancelBookingHistory"].count_documents({"advisorName": advisor_name}),
            # ✅ นับจำนวนที่ปิดการให้คำปรึกษาแล้ว (ทั้งอาจารย์กดปิดเองและระบบปิดอัตโนมัติ)
            lambda: db["QueueManagementHistory"].count_documents({
                "userId": advisor_id,
                "status": "Completed",
            }),
        )

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
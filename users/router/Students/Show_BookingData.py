import logging

from fastapi import APIRouter, HTTPException, Request, Depends
from ...Database.ConnectDB import Connect_MongoDB
from ...auth.authUser import verify_user_token, get_user_id

router = APIRouter()
logger = logging.getLogger(__name__)


def get_booking_by_user(user_id: str) -> list:
    try:
        db  = Connect_MongoDB()["BORC"]
        col = db["BookingOnline"]

        # ✅ ดึงเฉพาะข้อมูลของ user_id นี้เท่านั้น
        bookings = col.find(
            {"UserId": user_id},
            {"_id": 0}  # ไม่ส่ง _id กลับไป
        )

        return list(bookings)

    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดในการดึงข้อมูล: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )

@router.get("/ShowData")
async def Show_Data_BookingOnline(payload: dict = Depends(verify_user_token)):
    try:
        user_id  = payload["user_id"]
        bookings = get_booking_by_user(user_id)
        return {
            "user_id"  : user_id,
            "bookings" : bookings
        }
    

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")



@router.get("/BookingStats")
async def Get_Booking_Stats(payload: dict = Depends(verify_user_token)):
    try:
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db = Connect_MongoDB()["BORC"]

        # ✅ อ้างอิง rescheduledById + rescheduledByRole="Student" เท่านั้น
        total_rescheduled = db["RescheduleHistory"].count_documents({
            "rescheduledById" : user_id,
            "rescheduledByRole": "Student",
        })

        # ✅ อ้างอิง cancelledById + cancelledByRole="Student" เท่านั้น
        total_cancelled = db["CancelBookingHistory"].count_documents({
            "cancelledById"  : user_id,
            "cancelledByRole": "Student",
        })

        return {
            "stats": {
                "rescheduled": total_rescheduled,
                "cancelled"  : total_cancelled,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
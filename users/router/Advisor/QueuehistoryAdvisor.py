import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import get_current_advisor
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


@router.get("/All")
def get_all_queue_history(request: Request):
    try:
        payload = get_current_advisor(request)
        db = get_db()
        col = db["QueueManagementHistory"]

        Queues_history = list(col.find(
            {"userId" : payload["user_id"]},
            {"_id": 0}   # ไม่เอา _id มาเลย
        ))

        return {"data": Queues_history}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="เกิดข้อผิดพลาดภายในระบบ")
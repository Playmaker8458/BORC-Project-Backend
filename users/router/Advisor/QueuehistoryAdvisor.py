import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ...Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token
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


def get_current_advisor(request: Request) -> dict:
    payload = verify_user_token(request)
    if not payload or "user_id" not in payload:
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้องหรือหมดอายุ")
    if payload.get("role") != "Advisor":
        raise HTTPException(status_code=403, detail="ใช้งานได้เฉพาะอาจารย์เท่านั้น")
    return payload


@router.get("/All")
async def get_all_queue_history(request: Request):
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
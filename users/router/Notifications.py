import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ..Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_user_token, get_user_id
from common.notifications import get_notifications, mark_all_notifications_read

router = APIRouter()


# ─── GET /List ────────────────────────────────────────────────────────────────
@router.get("/List")
def list_notifications(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db = Connect_MongoDB()["BORC"]
        return get_notifications(db, user_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Notifications.List] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── POST /MarkAllRead ─────────────────────────────────────────────────────────
@router.post("/MarkAllRead")
def mark_all_read(request: Request):
    try:
        payload = verify_user_token(request)
        user_id = get_user_id(payload)
        if not user_id:
            raise HTTPException(status_code=401, detail="ไม่พบ user_id ใน token")

        db = Connect_MongoDB()["BORC"]
        mark_all_notifications_read(db, user_id)
        return {"message": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Notifications.MarkAllRead] %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

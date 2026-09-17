import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Request
from ..Database.ConnectDB import Connect_MongoDB
from users.auth.authUser import verify_pending_or_active_user

router = APIRouter()

@router.get('/LoginUser')
def LoginUser(request: Request):
    client = Connect_MongoDB()
    try:
        # ✅ ดึง user_id จาก cookie
        payload = verify_pending_or_active_user(request)
        user_id = payload["user_id"]

        db  = client["BORC"]
        col = db["UserProfile"]

        # ✅ ดึงเฉพาะข้อมูลของ user คนที่ Login อยู่
        user = col.find_one(
            { "userId": user_id },
            { "_id": 0, "userId": 1, "Prefix": 1, "Firstname": 1,
              "Lastname": 1, "Role": 1, "Status": 1 }
        )

        if not user:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ใช้")

        return user  # ✅ คืน object เดียว ไม่ใช่ array

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        client.close()

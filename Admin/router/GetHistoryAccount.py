import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException
from Admin.Database.ConnectDB import Connect_MongoDB
from Admin.auth.authAdmin import verify_token

router = APIRouter()

@router.get("/History")
def get_account_history(payload: dict = Depends(verify_token)):
    client = Connect_MongoDB()
    try:
        db = client["BORC"]
        col_history = db["AccountManagementHistory"]

        # เหตุการณ์ของบัญชีผู้ดูแลระบบ (kind="admin") เห็นได้เฉพาะผู้มีสิทธิ์ "เพิ่มบัญชีผู้ดูแลระบบ" (add)
        # คนอื่นเห็นเฉพาะประวัติการจัดการบัญชีผู้ใช้งานเหมือนเดิม
        query = {} if "add" in payload["permissions"] else {"kind": {"$ne": "admin"}}
        records = list(
            col_history.find(
                query,
                {"_id": 0, "Prefix": 1, "firstName": 1, "lastName": 1, "role": 1, "statusLabel": 1, "createdAt": 1,
                 "kind": 1, "actorEmail": 1, "permissions": 1, "level": 1}
            ).sort("createdAt", -1)   # ล่าสุดก่อน
        )
        return records

    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
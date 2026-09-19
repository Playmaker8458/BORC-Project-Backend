import logging

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException
from Admin.Database.ConnectDB import Connect_MongoDB

router = APIRouter()

@router.get("/History")
async def get_account_history():
    client = Connect_MongoDB()
    try:
        db = client["BORC"]
        col_history = db["AccountManagementHistory"]

        records = list(
            col_history.find(
                {},
                {"_id": 0, "firstName": 1, "lastName": 1, "role": 1, "statusLabel": 1, "createdAt": 1}
            ).sort("createdAt", -1)   # ล่าสุดก่อน
        )
        return records

    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
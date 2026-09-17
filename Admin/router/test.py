import logging

from fastapi import APIRouter, HTTPException
from ..Database.ConnectDB import Connect_MongoDB

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get('/CountUser')
async def Get_CountUser():
    try:
        Count_Student = []
        Count_Advisor = []

        db = Connect_MongoDB()["BORC"]
        col = db["UserProfile"]

        for item in col.find():
            if item["Role"] == "Student":
                Count_Student.append(item["Role"])
            elif item["Role"] == "Advisor":
                Count_Advisor.append(item["Role"])

        return {"CountStudent" : len(Count_Student), "CountAdvisor": len(Count_Advisor)}
    except Exception:
        logger.exception("Get_CountUser failed")
        raise HTTPException(status_code=500, detail="Internal server error")
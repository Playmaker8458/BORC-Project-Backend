import logging

from fastapi import APIRouter, HTTPException
from ..Database.ConnectDB import Connect_MongoDB

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get('/CountUser')
def Get_CountUser():
    """จำนวนนักศึกษา/อาจารย์ — ให้ MongoDB นับเอง (เดิมโหลดทุกเอกสารมานับในโค้ด และ KeyError ถ้าเอกสารไม่มี Role)"""
    try:
        col = Connect_MongoDB()["BORC"]["UserProfile"]
        return {
            "CountStudent": col.count_documents({"Role": "Student"}),
            "CountAdvisor": col.count_documents({"Role": "Advisor"}),
        }
    except Exception:
        logger.exception("Get_CountUser failed")
        raise HTTPException(status_code=500, detail="Internal server error")

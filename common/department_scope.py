"""กรองการจองตามสาขาวิชา: นักศึกษาเห็น/จองได้เฉพาะอาจารย์ (Role=Advisor) ที่ Department ตรงกัน

สาขาของผู้เรียก API อ่านจาก BORC.UserProfile เสมอ (ไม่รับจาก client และไม่ใช้ user cache)
เพื่อให้ค่าที่ Admin แก้ภายหลังมีผลทันที
"""
from typing import Iterable

from fastapi import HTTPException, status


def get_user_department(db, user_id: str) -> str:
    profile = db["UserProfile"].find_one({"userId": user_id}, {"_id": 0, "Department": 1})
    department = (profile or {}).get("Department")
    if not department:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ไม่พบข้อมูลสาขาวิชาของคุณ กรุณาติดต่อผู้ดูแลระบบ",
        )
    return department


def advisor_ids_in_department(db, department: str, advisor_ids: Iterable[str]) -> set[str]:
    """คืนเฉพาะ advisor id ที่เป็น Advisor และอยู่สาขา `department` (query เดียวไม่ว่ากี่คน)"""
    ids = list({i for i in advisor_ids if i})
    if not ids:
        return set()
    cursor = db["UserProfile"].find(
        {"userId": {"$in": ids}, "Role": "Advisor", "Department": department},
        {"_id": 0, "userId": 1},
    )
    return {doc["userId"] for doc in cursor}


def assert_same_department(db, user_id: str, advisor_id: str) -> None:
    """ใช้กับ AvailableSlots และ BookingOnline: ไม่ตรงสาขา -> 403"""
    department = get_user_department(db, user_id)
    if advisor_id not in advisor_ids_in_department(db, department, [advisor_id]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ไม่สามารถจองอาจารย์ท่านนี้ได้ เนื่องจากอยู่คนละสาขาวิชา",
        )
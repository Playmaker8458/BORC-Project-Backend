"""จำนวนข้อความแชทที่ยังไม่ได้อ่าน ต่อบทสนทนา (student_id, advisor_id) แบบ 1:1

ChatMessages ไม่มี field read/seen อยู่แล้ว (ดู users/router/Students/ChatStudent.py,
users/router/Advisor/ChatAdvisor.py) จึงไม่แก้ schema เดิม — เก็บแค่ ChatReadState เป็น
watermark "อ่านถึงเวลาไหนแล้ว" ต่อคู่ (readerId, counterpartId) แบบเดียวกับ
common/notifications.py (NotificationReadState) นับ unread จาก ChatMessages ที่ timestamp
ใหม่กว่า watermark และผู้ส่งไม่ใช่ตัวผู้อ่านเอง

ทำงานกับ AsyncMongoClient (db ของ ChatAdvisor.py/ChatStudent.py) จึงเป็น async ทั้งไฟล์
ต่างจาก common/notifications.py ที่ใช้ sync pymongo — คนละ client กัน
"""

from datetime import datetime, timezone

SENDER_FROM_ADVISOR = ["teacher", "advisor"]
SENDER_FROM_STUDENT = ["student"]


async def get_last_read_at(db, reader_id: str, counterpart_id: str):
    doc = await db["ChatReadState"].find_one({"readerId": reader_id, "counterpartId": counterpart_id})
    return doc.get("lastReadAt") if doc else None


async def get_unread_count(db, *, student_id: str, advisor_id: str, other_sender_values: list[str], last_read_at) -> int:
    query: dict = {
        "student_id": student_id,
        "advisor_id": advisor_id,
        "sender": {"$in": other_sender_values},
    }
    if last_read_at:
        query["timestamp"] = {"$gt": last_read_at}
    return await db["ChatMessages"].count_documents(query)


async def get_advisor_unread_counts(db, *, advisor_id: str, student_ids: list[str]) -> dict[str, int]:
    """เหมือน get_last_read_at + get_unread_count แต่ทำครั้งเดียวให้ทุกนักศึกษาของอาจารย์คนนี้

    get_last_read_at/get_unread_count เดิมต้อง await ทีละคน (2 round-trip ต่อคน) — ถ้าอาจารย์มี
    นักศึกษาหลายคน โพลทุก 15 วิจะยิง sequential round-trip ไป MongoDB Atlas เป็นสิบๆ ครั้งต่อครั้ง
    ทำให้ badge อัปเดตช้ากว่ารอบ poll จริงมาก (นี่คือจุดที่ทำให้ "ส่งช้า" ไม่ใช่ตัว interval เอง)
    ฟังก์ชันนี้ยุบเหลือ 2 round-trip เสมอไม่ว่าจะมีนักศึกษากี่คน: 1 query ดึง watermark ทุกคน
    + 1 aggregation นับ unread ทุกคนพร้อมกัน
    """
    if not student_ids:
        return {}

    read_states = await db["ChatReadState"].find(
        {"readerId": advisor_id, "counterpartId": {"$in": student_ids}}
    ).to_list(length=len(student_ids))
    last_read_by_student = {r["counterpartId"]: r.get("lastReadAt") for r in read_states}

    # เงื่อนไข timestamp ต่างกันตาม watermark ของแต่ละนักศึกษา จึงประกอบเป็น $or ต่อคน
    # แล้วนับรวมด้วย aggregation รอบเดียว แทนการ count_documents ทีละคน
    or_conditions = []
    for student_id in student_ids:
        condition: dict = {"student_id": student_id}
        last_read_at = last_read_by_student.get(student_id)
        if last_read_at:
            condition["timestamp"] = {"$gt": last_read_at}
        or_conditions.append(condition)

    pipeline = [
        {"$match": {
            "advisor_id": advisor_id,
            "sender": {"$in": SENDER_FROM_STUDENT},
            "$or": or_conditions,
        }},
        {"$group": {"_id": "$student_id", "count": {"$sum": 1}}},
    ]
    # ต่างจาก .find() (คืน cursor ทันทีแบบ sync) — .aggregate() ของ AsyncMongoClient เป็น
    # coroutine ต้อง await ก่อนถึงจะได้ cursor คืนมา (เดิมเรียก .to_list() ต่อท้ายทันทีโดยไม่ await
    # aggregate() ก่อน ได้ coroutine object ที่ไม่มี .to_list() → AttributeError → endpoint จับ
    # exception แล้วตอบ 500 → badge ฝั่งอาจารย์เลยไม่ขึ้นเลยสักครั้ง ทั้งที่ query ถูกต้อง)
    cursor = await db["ChatMessages"].aggregate(pipeline)
    rows = await cursor.to_list(length=len(student_ids))
    counts = {row["_id"]: row["count"] for row in rows}
    return {student_id: counts.get(student_id, 0) for student_id in student_ids}


async def mark_chat_read(db, reader_id: str, counterpart_id: str) -> None:
    await db["ChatReadState"].update_one(
        {"readerId": reader_id, "counterpartId": counterpart_id},
        {"$set": {"lastReadAt": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def ensure_chat_indexes(db) -> None:
    """สร้าง index ตอน startup (เรียกจาก main.lifespan)

    ChatMessages ไม่มี index มาตั้งแต่แรก (ทั้ง get_my_chat_history/get_chat_history เดิม
    และ endpoint unread ที่เพิ่มใหม่ ต่างก็ query/aggregate ด้วย collection scan ล้วนๆ) ยิ่งข้อความ
    เยอะขึ้นยิ่งช้า — ใส่ index ให้ทั้งสองรูปแบบ query ที่ใช้จริง:
    - (student_id, advisor_id, timestamp): ประวัติแชท + unread ฝั่งนักศึกษา (คู่เดียว)
    - (advisor_id, sender, timestamp): unread ฝั่งอาจารย์ (aggregate รวมหลายนักศึกษาต่อครั้ง)
    """
    await db["ChatReadState"].create_index(
        [("readerId", 1), ("counterpartId", 1)], unique=True, name="reader_counterpart_unique"
    )
    await db["ChatMessages"].create_index(
        [("student_id", 1), ("advisor_id", 1), ("timestamp", 1)], name="student_advisor_timestamp"
    )
    await db["ChatMessages"].create_index(
        [("advisor_id", 1), ("sender", 1), ("timestamp", 1)], name="advisor_sender_timestamp"
    )

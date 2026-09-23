"""แจ้งเตือนการจอง/จัดการคิว (อนุมัติ/ยกเลิก/เลื่อน/เสร็จสิ้น) สำหรับนักศึกษาและอาจารย์

ไม่เก็บเนื้อหาแจ้งเตือนเป็น collection ใหม่ — ใช้ QueueManagementHistory ที่ทุก endpoint
ที่เปลี่ยนสถานะคิวเขียนไว้อยู่แล้ว (ดู common/queue_history.py, common/booking_worker.py)
เก็บแค่ NotificationReadState (1 เอกสารต่อผู้ใช้) เป็น watermark ว่าอ่านถึงเวลาไหนแล้ว
แทนการมี flag read ต่อรายการ ลดความเสี่ยง count ไม่ตรงเวลามีคนเปิดพร้อมกันหลายแท็บ
และไม่ต้องแก้จุด insert เดิมที่กระจายอยู่หลายไฟล์
"""

from datetime import datetime, timezone

# ต้องตรงกับสถานะที่ log_queue_management_history / save_auto_queue_history เขียนไว้ (ดู
# users/router/Advisor/ManageQueueAdvisor.py, users/router/Students/Reschedule_Students.py,
# users/router/Advisor/Reschedule_Advisor.py, common/booking_worker.py) — "Pending" ไม่มีที่มา
# เพราะการจองใหม่ยังไม่ถูกเขียนลง QueueManagementHistory (ดูหมายเหตุใน get_notifications ด้านล่าง)
NOTIFY_STATUS_MESSAGE = {
    "Approved": "คิวได้รับการอนุมัติแล้ว",
    "Rescheduled": "มีการเลื่อนนัดหมาย กรุณาตรวจสอบเวลาใหม่",
    "Completed": "การให้คำปรึกษาเสร็จสิ้นแล้ว",
    "Cancelled": "คิวถูกยกเลิก",
}

MAX_NOTIFICATIONS = 30


def get_notifications(db, user_id: str) -> dict:
    read_state = db["NotificationReadState"].find_one({"userId": user_id})
    last_read_at = read_state.get("lastReadAt") if read_state else None

    docs = list(
        db["QueueManagementHistory"]
        .find({"userId": user_id})
        .sort("createdAt", -1)
        .limit(MAX_NOTIFICATIONS)
    )

    notifications = []
    unread_count = 0
    for doc in docs:
        message = NOTIFY_STATUS_MESSAGE.get(doc.get("status", ""))
        if not message:
            continue
        created_at = doc.get("createdAt")
        is_read = bool(last_read_at and created_at and created_at <= last_read_at)
        if not is_read:
            unread_count += 1
        notifications.append({
            "id": str(doc["_id"]),
            "title": doc.get("UserName", ""),
            "message": message,
            "status": doc.get("status", ""),
            "createdAt": created_at,
            "read": is_read,
        })

    return {"notifications": notifications, "unreadCount": unread_count}


def mark_all_notifications_read(db, user_id: str) -> None:
    db["NotificationReadState"].update_one(
        {"userId": user_id},
        {"$set": {"lastReadAt": datetime.now(timezone.utc)}},
        upsert=True,
    )

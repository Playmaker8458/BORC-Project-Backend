"""สถานะของคิว (BookingOnline.Status) และกลุ่มสถานะที่ใช้ร่วมกันทั้งระบบ — แหล่งเดียวของค่าเหล่านี้

วงจรชีวิตของคิว:

    Pending ──อาจารย์อนุมัติ──▶ Approved ──ถึงเวลานัด──▶ InProgress ──หมดเวลา/อาจารย์ปิด──▶ Completed
       │                          │  ▲
       │                          │  └──อาจารย์อนุมัติเวลาใหม่──┐
       │                          └──เลื่อนคิว (นักศึกษา/อาจารย์)──▶ Rescheduled
       └──────────ยกเลิก (Pending/Approved/Rescheduled) หรือระบบยกเลิกเมื่อไม่อนุมัติก่อนนัด 1 ชม.──▶ Cancelled

เดิมกลุ่มเหล่านี้ประกาศซ้ำในทุกไฟล์ (บางไฟล์ใช้ชื่อเดียวกันแต่ความหมายต่างกัน) จึงรวมไว้ที่นี่ที่เดียว
ห้ามแก้ list เหล่านี้ระหว่างรัน (ใช้ร่วมกันทั้ง process)
"""

# คิวที่ยังใช้งานอยู่: นักศึกษามีได้ทีละ 1 คิว และคิวเหล่านี้ล็อก slot ของอาจารย์ไว้
ACTIVE_STATUSES = ["Pending", "Approved", "InProgress", "Rescheduled"]

# ยกเลิกได้ (InProgress เริ่มไปแล้ว ยกเลิกไม่ได้)
CANCELLABLE_STATUSES = ["Pending", "Approved", "Rescheduled"]

# อาจารย์อนุมัติได้: จองใหม่ หรือนักศึกษาเลื่อนมาแล้วรออนุมัติเวลาใหม่
CONFIRMABLE_STATUSES = ["Pending", "Rescheduled"]

# อาจารย์ปิดการให้คำปรึกษาได้ (Approved ต้องถึงเวลานัดแล้ว — เช็กแยกที่ CompleteQueue)
COMPLETABLE_STATUSES = ["Approved", "InProgress"]

# แชทได้: คิวที่ active และคิวที่เพิ่งเสร็จสิ้น (ความหมายต่างจาก ACTIVE_STATUSES — เดิมสองอย่างใช้ชื่อเดียวกัน)
CHAT_STATUSES = ACTIVE_STATUSES + ["Completed"]

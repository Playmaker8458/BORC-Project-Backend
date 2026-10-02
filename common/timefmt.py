"""ส่งเวลาออกทาง API เป็นเวลาไทย (UTC+7) พร้อม offset ชัดเจน เช่น "2026-10-02T12:48:57.574000+07:00"

ฐานข้อมูลเก็บ datetime เป็น UTC เสมอ (MongoDB เก็บวันเวลาเป็นช่วงเวลาสากล และ datetime.now(timezone.utc) ในโค้ด) — ไม่เปลี่ยน
แต่ pymongo คืนค่าแบบ "ไม่มีเขตเวลา" (naive) และ FastAPI จะส่ง "2026-10-02T05:48:57" ซึ่งไม่บอกว่าเป็นเขตเวลาไหน
เบราว์เซอร์อ่านเป็นเวลาท้องถิ่น แสดงผิดไป 7 ชั่วโมงสำหรับผู้ใช้ในไทย ฟังก์ชันนี้จึงตีความ naive เป็น UTC แล้วส่งออกเป็นเวลาไทยพร้อม "+07:00"
(เป็นช่วงเวลาเดียวกับ UTC เป๊ะ ผู้รับที่อ่าน ISO 8601 ได้ แปลงกลับเป็น UTC ได้ถูกต้อง รวมถึงหน้าเว็บ — src/lib/format.ts)
"""

from datetime import datetime, timedelta, timezone
from typing import Any

THAI_TZ = timezone(timedelta(hours=7))


def to_thai_iso(value: Any) -> Any:
    """datetime → ISO 8601 เวลาไทยลงท้าย +07:00 (naive ถือเป็น UTC); ค่าอื่น (None, str ที่แปลงแล้ว ฯลฯ) คืนค่าเดิม"""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(THAI_TZ).isoformat()


def install_thai_json_encoder() -> None:
    """ทำให้ FastAPI ส่ง datetime ทุกตัวเป็นเวลาไทย +07:00 (เรียกครั้งเดียวตอนสร้างแอปใน main.py; เรียกซ้ำได้)

    เป็นตัวป้องกันกลาง: endpoint ที่คืน datetime ตรงๆ (เช่น คืนเอกสารจาก MongoDB ทั้งก้อน) จะไม่กลับมาส่งเวลาแบบไม่มี offset อีก
    endpoint ที่แปลงเองอยู่แล้ว (ประวัติคิว/แจ้งเตือน/แชท) ไม่ได้รับผลกระทบ เพราะค่าเป็น str ที่แปลงแล้ว
    """
    from fastapi import encoders

    encoders.ENCODERS_BY_TYPE[datetime] = to_thai_iso

"""ความยินยอมนโยบายความเป็นส่วนตัว (PDPA)

CURRENT_CONSENT_VERSION ต้องตรงกับ PRIVACY_POLICY_VERSION ฝั่ง frontend
(newbooking-frontend-react: src/features/privacy/constants/config.ts) — เปลี่ยนทั้งสองที่พร้อมกันเมื่อนโยบายเปลี่ยน
ผู้ใช้ที่ consentVersion ไม่ตรงกับค่านี้จะถูก frontend ขอความยินยอมใหม่ที่หน้าหลัก

เวลาที่ยินยอม (consentedAt) backend บันทึกจากเวลาเซิร์ฟเวอร์เสมอ ไม่รับจาก client
"""

from datetime import datetime, timezone

CURRENT_CONSENT_VERSION = "1.0"


def validate_consent_version(value: str) -> str:
    value = value.strip()
    if value != CURRENT_CONSENT_VERSION:
        raise ValueError("เวอร์ชันนโยบายความเป็นส่วนตัวไม่ถูกต้อง กรุณารีเฟรชหน้าแล้วลองใหม่")
    return value


def consent_fields(now: datetime | None = None) -> dict:
    """ฟิลด์ที่บันทึกลง UserProfile เมื่อผู้ใช้ยินยอมนโยบายเวอร์ชันปัจจุบัน"""
    return {
        "consentVersion": CURRENT_CONSENT_VERSION,
        "consentedAt": now or datetime.now(timezone.utc),
    }


def consented_at_iso(value) -> str | None:
    """แปลงเวลาที่ยินยอมเป็น ISO 8601 (UTC) สำหรับส่งให้ frontend; ไม่มีค่า → None"""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:  # pymongo คืน datetime แบบ naive (เก็บเป็น UTC)
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

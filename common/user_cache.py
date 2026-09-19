"""Cache ข้อมูลผู้ใช้ (UserProfile) แบบอายุสั้นในหน่วยความจำ

verify_user_token ทำงานทุก request; เดิมยิง MongoDB Atlas 1 รอบต่อ request (ข้ามเครือข่าย
Railway -> Atlas) จึง cache ไว้ _TTL_SEC วินาที ผลข้างเคียง: การเปลี่ยน Role/Status/ชื่อของผู้ใช้จะมีผล
ภายใน _TTL_SEC วินาที **เว้นแต่** โค้ดที่แก้ UserProfile เรียก invalidate_user_cache(user_id) หลังแก้
(Admin แก้/ลบบัญชี, ผู้ใช้แก้ชื่อ/รูป) ซึ่งทำให้มีผลทันทีใน process เดียวกัน
(ถ้ารันหลาย worker/instance การล้างจะมีผลเฉพาะ process นั้น ส่วนที่เหลือรอ TTL หมดอายุ)
"""

import time

_TTL_SEC = 15
_MAX_ENTRIES = 2048
_cache: dict[str, tuple[float, dict]] = {}


def cache_get(user_id: str) -> dict | None:
    hit = _cache.get(user_id)
    if hit and time.monotonic() - hit[0] < _TTL_SEC:
        return hit[1]
    return None


def cache_put(user_id: str, user: dict) -> None:
    if len(_cache) >= _MAX_ENTRIES:
        _cache.clear()
    _cache[user_id] = (time.monotonic(), user)


def invalidate_user_cache(user_id: str | None = None) -> None:
    """ล้าง cache ของผู้ใช้คนเดียว (หรือทั้งหมดถ้าไม่ระบุ)"""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)

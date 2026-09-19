"""ตรวจความปลอดภัยของ URL ที่ผู้ใช้ส่งมา (เช่น ลิงก์นัดหมายที่อาจารย์ส่งให้นักศึกษา)

กันการส่ง javascript:/data:/http:, URL ที่ฝัง user:pass@host (หลอกให้เห็นเป็นโดเมนอื่น),
โดเมนที่มีตัวอักษรที่ไม่ใช่ ASCII (homograph) และค่าที่มีช่องว่าง/อักขระควบคุม
"""

from collections.abc import Iterable
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


class UnsafeUrl(ValueError):
    """URL ไม่ผ่านการตรวจ (ข้อความในตัว exception เป็นภาษาไทยพร้อมแสดงให้ผู้ใช้)"""


def validate_https_url(value: str, allowed_hosts: Iterable[str] | None = None) -> str:
    """คืน URL ที่ตัดช่องว่างหัวท้ายแล้ว หรือ raise UnsafeUrl

    allowed_hosts: ถ้าระบุ hostname ต้องตรงหรือเป็นโดเมนย่อยของรายการใดรายการหนึ่ง
    (เทียบที่ขอบจุด: "zoom.us" ผ่าน "us02web.zoom.us" แต่ไม่ผ่าน "evilzoom.us")
    """
    url = (value or "").strip()
    if not url:
        raise UnsafeUrl("กรุณาระบุลิงก์")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrl("ลิงก์ยาวเกินไป")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise UnsafeUrl("ลิงก์ต้องไม่มีช่องว่างหรืออักขระควบคุม")

    try:
        parts = urlsplit(url)
        _ = parts.port  # ค่า port ที่ผิดรูปแบบจะ raise ValueError
    except ValueError:
        raise UnsafeUrl("รูปแบบลิงก์ไม่ถูกต้อง") from None

    if parts.scheme.lower() != "https":
        raise UnsafeUrl("ลิงก์ต้องขึ้นต้นด้วย https://")

    host = parts.hostname
    if not host:
        raise UnsafeUrl("ลิงก์ไม่มีชื่อโดเมน")
    if "@" in parts.netloc:
        raise UnsafeUrl("ลิงก์ต้องไม่มีชื่อผู้ใช้/รหัสผ่านฝังอยู่")
    if not host.isascii():
        raise UnsafeUrl("ชื่อโดเมนต้องเป็นตัวอักษร ASCII")

    if allowed_hosts:
        host_l = host.lower()
        allowed = [h.strip().lower().lstrip(".") for h in allowed_hosts if h and h.strip()]
        if allowed and not any(host_l == a or host_l.endswith("." + a) for a in allowed):
            raise UnsafeUrl("โดเมนของลิงก์ไม่อยู่ในรายการที่อนุญาต")

    return url

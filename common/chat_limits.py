"""จำกัดขนาดและความถี่ของข้อความแชท (กันผู้ใช้ที่ล็อกอินแล้วอัดข้อมูลเข้า DB / ยิงรัว)

ค่าเริ่มต้น (ปรับได้ด้วย env):
  CHAT_MAX_TEXT_LENGTH  = 2000 ตัวอักษรต่อข้อความ (1..20000)
  CHAT_RATE_MAX         = 15 ข้อความ
  CHAT_RATE_WINDOW_SEC  = 10 วินาที
ตัวนับความถี่อยู่ในหน่วยความจำของ process เดียว (เหมือน `rooms` ของแชท) ถ้าอนาคตรันหลาย
worker/instance จะนับแยกกัน ต้องย้ายไปเก็บที่กลางเช่น Redis

รหัสปิด WebSocket ที่ใช้: 1009 = ข้อความใหญ่เกิน, 1007 = รูปแบบข้อมูลไม่ถูกต้อง, 1008 = ส่งถี่เกิน
"""

import json
import os
import time
from collections import deque

DEFAULT_MAX_TEXT_LENGTH = 2000
DEFAULT_RATE_MAX = 15
DEFAULT_RATE_WINDOW_SEC = 10

WS_TOO_BIG = 1009
WS_INVALID_PAYLOAD = 1007
WS_POLICY_VIOLATION = 1008


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if low <= value <= high else default


def max_text_length() -> int:
    return _int_env("CHAT_MAX_TEXT_LENGTH", DEFAULT_MAX_TEXT_LENGTH, 1, 20000)


def rate_config() -> tuple[int, int]:
    return (
        _int_env("CHAT_RATE_MAX", DEFAULT_RATE_MAX, 1, 1000),
        _int_env("CHAT_RATE_WINDOW_SEC", DEFAULT_RATE_WINDOW_SEC, 1, 3600),
    )


class ChatInvalid(Exception):
    """ข้อความแชทไม่ผ่านการตรวจ; code = รหัสปิด WebSocket, str(exc) = ข้อความภาษาไทย"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def parse_ws_text(raw: str) -> str | None:
    """ตรวจเฟรมข้อความจาก WebSocket คืนข้อความ หรือ None ถ้าเป็นข้อความว่าง (ให้ข้ามไป); raise ChatInvalid"""
    limit = max_text_length()
    # เฟรมใหญ่เกินเหตุ: ปฏิเสธก่อน parse JSON (ตัวอักษรหนึ่งตัวใน JSON ยาวสุด 6 ไบต์ เช่น \uXXXX)
    if len(raw) > limit * 6 + 256:
        raise ChatInvalid(WS_TOO_BIG, "ข้อความยาวเกินไป")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ChatInvalid(WS_INVALID_PAYLOAD, "รูปแบบข้อความไม่ถูกต้อง") from None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise ChatInvalid(WS_INVALID_PAYLOAD, "รูปแบบข้อความไม่ถูกต้อง")
    text = data["text"]
    if len(text) > limit:
        raise ChatInvalid(WS_TOO_BIG, f"ข้อความยาวเกิน {limit} ตัวอักษร")
    return text if text.strip() else None


def check_rest_text(text: str) -> str:
    """ตรวจข้อความจาก REST คืนข้อความเดิม; raise ChatInvalid (ใช้ str(exc) เป็นข้อความ error)"""
    limit = max_text_length()
    if not text or not text.strip():
        raise ChatInvalid(WS_INVALID_PAYLOAD, "ข้อความต้องไม่ว่าง")
    if len(text) > limit:
        raise ChatInvalid(WS_TOO_BIG, f"ข้อความยาวเกิน {limit} ตัวอักษร")
    return text


class SlidingWindowLimiter:
    """นับจำนวนเหตุการณ์ในช่วงเวลาเลื่อน (sliding window) ต่อกุญแจ เช่น user id"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._events: dict[str, deque] = {}

    def allow(self, key: str, max_events: int, window_sec: float) -> bool:
        now = self._clock()
        cutoff = now - window_sec
        if len(self._events) > 100:  # เก็บกวาดกุญแจที่ไม่มีการใช้งานแล้ว กันหน่วยความจำโตไม่จำกัด
            for k in [k for k, q in self._events.items() if not q or q[-1] <= cutoff]:
                del self._events[k]
        q = self._events.setdefault(key, deque())
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= max_events:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        self._events.clear()


chat_limiter = SlidingWindowLimiter()


def chat_rate_ok(user_id: str) -> bool:
    """True ถ้าผู้ใช้ยังส่งข้อความ/ลิงก์ได้ (นับรวมทั้ง WebSocket และ REST)"""
    max_events, window = rate_config()
    return chat_limiter.allow(user_id, max_events, window)


def retry_after_seconds() -> int:
    return rate_config()[1]

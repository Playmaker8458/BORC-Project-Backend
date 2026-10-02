"""ไม่ให้ตั๋ว WebSocket (?ticket=...) ไปอยู่ใน access log

ตั๋วอยู่ใน query string ของ URL (WebSocket ของเบราว์เซอร์ส่ง header เองไม่ได้) และ uvicorn บันทึก path พร้อม query ลง log
ตั๋วอายุแค่ 30 วินาที แต่ก็ไม่ควรค้างอยู่ใน log — แทนค่าเป็น [redacted] ก่อนถูกเขียน
"""

import logging
import re

_TICKET_IN_URL = re.compile(r"([?&]ticket=)[^&\s\"']+")


def redact_ticket(text: str) -> str:
    return _TICKET_IN_URL.sub(r"\1[redacted]", text)


class RedactTicketFilter(logging.Filter):
    """แก้ข้อความ log ที่มี ?ticket=... (รวมค่าใน args ของ uvicorn.access) แล้วปล่อยผ่านเสมอ"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_ticket(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_ticket(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_ticket(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True

"""ส่งแจ้งเตือนไปยัง ChatBot service (LINE) แบบ synchronous

รวม logic ที่เคยกระจายซ้ำกันในหลายไฟล์ (timeout, header, try/except กลืน error
แล้ว log warning) — การแจ้งเตือนล้มเหลวไม่ควรทำให้ request หลัก
(จองคิว/อนุมัติ/ยกเลิก/เลื่อนคิว) ล้มเหลวตามไปด้วย จึงกลืน exception ทั้งหมดไว้ที่นี่
"""

import logging

import requests

logger = logging.getLogger(__name__)


def notify_chatbot(url: str, payload: dict, headers: dict, timeout: int = 5):
    """POST แจ้งเตือนไปที่ url ที่ระบุ คืน Response ถ้าสำเร็จ หรือ None ถ้าล้มเหลว"""
    try:
        return requests.post(url, json=payload, timeout=timeout, headers=headers)
    except Exception as e:
        logger.warning(f"[WARN] แจ้งเตือนไปยัง {url} ล้มเหลว: {e}")
        return None

"""
tests/conftest.py

ตั้งค่า environment variables จำลอง "ก่อน" import โมดูลใด ๆ ของแอป เพราะ
users/auth/authUser.py จะ raise RuntimeError ตอน import ถ้าไม่มี
JWT_SECRET_KEY / LINE_LOGIN_* ใน environment (fail-fast check ของโปรเจกต์)

ชุดทดสอบทั้งหมดใช้ mongomock แทน MongoDB จริง จึงไม่ต้องมี MongoDB รันอยู่
"""

import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("MONGODB_ALART_CLIENT_URL", "mongodb://localhost:27017")
os.environ.setdefault("LINE_LOGIN_CHANNEL_ID", "test-line-channel-id")
os.environ.setdefault("LINE_LOGIN_CHANNEL_SECRET", "test-line-channel-secret")
os.environ.setdefault("LINE_LOGIN_REDIRECT_URI", "http://localhost/line/callback")
os.environ.setdefault("ChatBot_URL", "http://localhost:5000")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test-cloud")
os.environ.setdefault("CLOUDINARY_API_KEY", "test-key")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test-secret")
os.environ.setdefault("ENV", "test")

import pytest
import mongomock
from mongomock.gridfs import enable_gridfs_integration

# ไฟล์แนบเก็บใน GridFS (common/attachments.py) — ทำให้ gridfs.GridFS ใช้กับ mongomock ได้ในชุดทดสอบ
enable_gridfs_integration()


@pytest.fixture
def mongo_client():
    """mongomock client ที่ใช้แทน pymongo.MongoClient ในชุดทดสอบ"""
    return mongomock.MongoClient()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """common/rate_limit.py's `limiter` เป็น module-level singleton ที่ใช้ร่วมกันทุก
    test — ถ้าไม่ reset ระหว่างเทส จำนวน request สะสมข้าม test จะชนเพดาน 5/minute
    ของ endpoint login ทำให้เทสที่ไม่เกี่ยวกับ rate limit ล้มเหลวแบบสุ่มขึ้นกับลำดับรัน"""
    from common.rate_limit import limiter

    if limiter is not None:
        limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _reset_user_cache():
    """verify_user_token cache ข้อมูลผู้ใช้ไว้ในหน่วยความจำ (ข้าม request) — ล้างก่อน/หลังทุกเทส
    ไม่ให้ผู้ใช้ชื่อเดียวกันจากเทสก่อนหน้า (คนละ mongomock) รั่วมาเป็นผลของเทสถัดไป"""
    from users.auth import authUser

    authUser.invalidate_user_cache()
    yield
    authUser.invalidate_user_cache()


@pytest.fixture(autouse=True)
def _reset_chat_limiter():
    """ตัวนับความถี่ของแชทเป็น state ระดับ process (ข้ามคำขอ) — ล้างก่อน/หลังทุกเทส
    ไม่ให้ผู้ใช้ชื่อเดียวกันจากเทสก่อนหน้าทำให้เทสถัดไปโดน 429/ปิด WebSocket แบบสุ่มตามลำดับรัน"""
    from common.chat_limits import chat_limiter

    chat_limiter.reset()
    yield
    chat_limiter.reset()

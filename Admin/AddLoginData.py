import logging
import os

from dotenv import load_dotenv
from Database.ConnectDB import Connect_MongoDB
from password_check import hash_password

# สิทธิ์ทั้งหมดต้องตรงกับ ADMIN_PERMISSIONS ใน Admin/auth/authAdmin.py (ไฟล์นี้รันเดี่ยวก่อน uvicorn จึงไม่ import ข้ามแพ็กเกจ)
ADMIN_PERMISSIONS = ("verify", "manage", "history", "add")
TOP_ADMIN_LEVEL = "high"  # ป้ายผู้ดูแลระบบหลัก (กำหนดให้เลือกเองไม่ได้ — ADMIN_LEVELS ใน admin_account_rules.py มีแค่ mid/low)

# เข้าถึงตัวแปลในไฟล์ .env เพื่อดึงมาใช้งานในไฟล์ AddLoginData.py แบบ Local
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ต้องตรงกับกติกาตอนเปลี่ยนรหัสผ่านแอดมิน (Admin/router/SettingAdmin.py: MIN_PASSWORD_LENGTH)
# และขีดจำกัดของ bcrypt (72 ไบต์)
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72


def load_seed_credentials(env=None) -> tuple[str, str]:
    """อ่านอีเมล/รหัสผ่านแอดมินตัวแรกจาก EMAIL_LOGIN / PASSWORD_LOGIN

    ขาดหรือรหัสผ่านไม่ผ่านกติกา = หยุดพร้อมข้อความที่บอกสาเหตุ (เดิมเป็น AttributeError ของ None.encode
    ที่ไม่บอกอะไร) — Dockerfile รันไฟล์นี้ก่อน uvicorn ด้วย && จึงเป็นการหยุดที่ตั้งใจ ไม่ใช่การล้มเงียบ
    """
    env = os.environ if env is None else env
    email = (env.get("EMAIL_LOGIN") or "").strip()
    password = env.get("PASSWORD_LOGIN") or ""

    missing = [name for name, value in (("EMAIL_LOGIN", email), ("PASSWORD_LOGIN", password)) if not value]
    if missing:
        raise SystemExit(
            f"ยังไม่ได้ตั้งค่า {', '.join(missing)} ใน environment/.env — "
            "ต้องมีอีเมลและรหัสผ่านของแอดมินตัวแรกก่อน (ใช้เฉพาะตอนยังไม่มีแอดมินในระบบ)"
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"PASSWORD_LOGIN สั้นเกินไป: ต้องมีอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise SystemExit(f"PASSWORD_LOGIN ยาวเกินไป: ต้องไม่เกิน {MAX_PASSWORD_BYTES} ไบต์ (ตัวอักษรไทยนับ 3 ไบต์ต่อตัว)")
    return email, password


# Add_LoginData ใช้สำหรับเพิ่มข้อมูลการเข้าสู่ระบบของผู้ดูแลระบบ
def Add_LoginData(email: str, plain_password: str) -> bool:
    # MongoDB Database Connect
    myclient = Connect_MongoDB() # เชื่อมต่อ MongoDB Database
    mydb = myclient["BORC"] # เข้าถึงฐานข้อมูล BORC
    mycol = mydb["LoginAdmin"] # เข้าถึงตาราง LoginAdmin ในฐานข้อมูล BORC

    if mycol.find_one() is not None:
        logger.info("ไม่สามารถข้อมูลในตาราง LoginAdmin ได้เนื่องจากมีข้อมูลอยู่แล้ว")
        return False

    # เข้ารหัสผ่านด้วย bcrypt ในไฟล์ password_check (ทำเฉพาะตอนต้องสร้างจริง)
    # บัญชีแรก = ผู้ดูแลระบบหลัก: เก็บสิทธิ์ครบทุกข้อและป้ายระดับไว้ในฐานข้อมูลตั้งแต่ต้น ไม่พึ่งการ "ไม่มีฟิลด์ = ได้ทุกสิทธิ์"
    mycol.insert_one({
        "Email": email,
        "Password": hash_password(plain_password),
        "Permissions": list(ADMIN_PERMISSIONS),
        "Level": TOP_ADMIN_LEVEL,
    })
    logger.info("เพิ่มข้อมูลในตาราง LoginAdmin ในฐานข้อมูล BORC สำเร็จแล้ว")
    return True


def backfill_legacy_admins() -> int:
    """เติม Permissions (ครบทุกสิทธิ์ ตรงกับความหมายเดิมของ "ไม่มีฟิลด์") ให้แอดมินเดิมที่ยังไม่มี
    และเติม Level ให้เฉพาะบัญชีแรกสุดของระบบที่ยังไม่มีทั้งสองฟิลด์ ทำซ้ำได้ ไม่ทับค่าที่ตั้งไว้แล้ว"""
    col = Connect_MongoDB()["BORC"]["LoginAdmin"]
    legacy = {"$or": [{"Permissions": {"$exists": False}}, {"Permissions": None}]}

    first = col.find_one({}, sort=[("_id", 1)])
    if first is not None and "Level" not in first and (first.get("Permissions") is None):
        col.update_one({"_id": first["_id"]}, {"$set": {"Level": TOP_ADMIN_LEVEL}})

    result = col.update_many(legacy, {"$set": {"Permissions": list(ADMIN_PERMISSIONS)}})
    if result.modified_count:
        logger.info("เติมสิทธิ์ให้แอดมินเดิม %d บัญชี", result.modified_count)
    return result.modified_count


def main(env=None) -> bool:
    """seed แอดมินตัวแรก; ถ้ามีแอดมินอยู่แล้วไม่ต้องมี EMAIL_LOGIN/PASSWORD_LOGIN เลย (เดิมต้องมีทุกครั้งที่ container เริ่ม)"""
    if Connect_MongoDB()["BORC"]["LoginAdmin"].find_one() is not None:
        logger.info("มีแอดมินอยู่ในระบบแล้ว ข้ามการ seed")
        backfill_legacy_admins()
        return False
    email, password = load_seed_credentials(env)
    return Add_LoginData(email, password)


if __name__ == "__main__":
    main()

import logging
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from fastapi import APIRouter, HTTPException, Depends, Request, Response
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from bson import ObjectId
from bson.errors import InvalidId

from Admin.Database.ConnectDB import Connect_MongoDB
from common.cookies import cookie_security_flags
from common.jwt_utils import encode_token, decode_token, JWTError
from common.rate_limit import limit
import bcrypt

logger = logging.getLogger(__name__)

# สร้าง router (แทน blueprint ของ fast API)
router = APIRouter()

# ตั้งค่าการสร้าง JWT (การเข้ารหัส/ถอดรหัสจริงอยู่ใน common/jwt_utils.py)
SESSION_MAX_AGE = timedelta(hours=24)  # 24 ชั่วโมง
# เข้าถึงตัวแปรในไฟล์ .env เพื่อดึงมาใช้งานในไฟล์ auth.py แบบ local
load_dotenv()

# session ของแอดมิน: คุกกี้ httpOnly (JavaScript อ่านไม่ได้ — เดิมหน้าเว็บเก็บ token ใน localStorage ซึ่ง XSS ขโมยได้)
# ยังรับ Authorization: Bearer ด้วยเพื่อ client/สคริปต์เดิม; ถ้ามีทั้งสองอย่าง Bearer มาก่อน
ADMIN_COOKIE_NAME = "admin_token"
# เบราว์เซอร์หนึ่งตัวล็อกอินแอดมินได้หลายบัญชีพร้อมกัน: แต่ละบัญชีมีคุกกี้ของตัวเอง (admin_token_<id>) และแต่ละแท็บบอกว่าตัวเองคือบัญชีไหน
# ผ่าน header X-Admin-Account (ค่าเก็บใน sessionStorage ของแท็บ) — header ใช้เลือกคุกกี้เท่านั้น ไม่ใช่หลักฐานตัวตน
# ตัวตนยังมาจาก JWT ในคุกกี้เสมอ และ user_id ใน token ต้องตรงกับ header ไม่งั้น 401
# คุกกี้เดิม (admin_token) ยังตั้งไว้เป็นบัญชีที่ล็อกอินล่าสุด ให้แท็บ/ไคลเอนต์รุ่นเก่าที่ไม่ส่ง header ใช้งานต่อได้เหมือนเดิม
ADMIN_ACCOUNT_HEADER = "X-Admin-Account"
_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/authAdmin/Login", auto_error=False)

# MODEL รูปแบบข้อมูลที่ client ต้องส่งมา
class LoginRequest(BaseModel):
    email: str
    password: str

# ฟังก์ชันสร้าง JWT token
def create_access_token(data: dict):
    """
    รับข้อมูล user แล้วสร้าง token พร้อมวันหมดอายุ
    (ใช้ common.jwt_utils.encode_token ร่วมกับฝั่ง users/auth/authUser.py)

    ใส่ iat (เวลาออก token แบบทศนิยมวินาที กันช่องว่างเมื่อเปลี่ยนรหัสผ่านในวินาทีเดียวกับที่ล็อกอิน) ด้วย เพื่อให้ verify_token ตรวจได้ว่า token ออกก่อน
    การเปลี่ยนรหัสผ่านล่าสุดหรือไม่ (ถ้าใช่ = ถูกเพิกถอน)
    """
    return encode_token({**data, "iat": datetime.now(timezone.utc).timestamp()},
                        SESSION_MAX_AGE)


# hash ปลอมไว้เทียบรหัสผ่านตอนไม่พบอีเมล: ให้เวลาตอบเท่ากับกรณีมีอีเมล (กันเดาอีเมลจากเวลาตอบ)
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode("utf-8")
INVALID_CREDENTIALS_DETAIL = "อีเมลหรือรหัสผ่านไม่ถูกต้อง"
MAX_PASSWORD_BYTES = 72  # ขีดจำกัดของ bcrypt


# สิทธิ์รายหน้าของแอดมิน เก็บเป็นลิสต์ในฟิลด์ Permissions ของ LoginAdmin
#   verify=ยืนยันการเข้าสู่ระบบ, manage=จัดการบัญชี, history=ประวัติจัดการบัญชี, add=เพิ่มบัญชีผู้ดูแลระบบ
ADMIN_PERMISSIONS = ("verify", "manage", "history", "add")


def get_admin_permissions(admin: dict) -> list[str]:
    """แอดมินเดิมที่ไม่มีฟิลด์ Permissions ได้ทุกสิทธิ์ (ไม่ให้ระบบเดิมพัง); ค่าที่ไม่รู้จักถูกตัดทิ้ง"""
    granted = admin.get("Permissions")
    if granted is None:
        return list(ADMIN_PERMISSIONS)
    return [p for p in ADMIN_PERMISSIONS if p in granted]


def _to_epoch(value: datetime) -> float:
    """MongoDB คืน datetime แบบไม่มี timezone (UTC) ถ้าไม่ได้ตั้ง tz_aware"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def admin_cookie_name(user_id: str) -> str:
    return f"{ADMIN_COOKIE_NAME}_{user_id}"


def set_admin_cookie(request: Request, response: Response, token: str) -> None:
    """ตั้งคุกกี้ session แอดมิน (อายุเท่ากับ JWT) — secure/samesite ตามว่าเป็น HTTPS หรือไม่ เหมือนคุกกี้ผู้ใช้
    ตั้งสองอัน: คุกกี้ของบัญชีนี้ (admin_token_<id>) และคุกกี้เดิม (admin_token = บัญชีที่ล็อกอินล่าสุด)"""
    user_id = decode_token(token)["user_id"]
    for key in (admin_cookie_name(user_id), ADMIN_COOKIE_NAME):
        response.set_cookie(
            key=key,
            value=token,
            httponly=True,
            max_age=int(SESSION_MAX_AGE.total_seconds()),
            path="/",
            **cookie_security_flags(request),
        )


def requested_account(request: Request) -> str | None:
    """บัญชีที่แท็บนี้ระบุผ่าน header (None = ไม่ระบุ → ใช้คุกกี้เดิม); รูปแบบผิดถือเป็น 401"""
    raw = request.headers.get(ADMIN_ACCOUNT_HEADER)
    if raw is None:
        return None
    if not _ACCOUNT_ID_PATTERN.match(raw):
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้อง")
    return raw


def _legacy_cookie_belongs_to(request: Request, account: str) -> bool:
    """คุกกี้เดิม (admin_token) เป็นของบัญชีนี้หรือเสียอยู่แล้วหรือไม่ (ถ้าใช่ ควรล้างทิ้งพร้อมกัน)"""
    legacy = request.cookies.get(ADMIN_COOKIE_NAME)
    if not legacy:
        return False
    try:
        return decode_token(legacy).get("user_id") == account
    except JWTError:
        return True


def _clear_admin_cookies(request: Request, response: Response, account: str | None) -> None:
    """ล้างคุกกี้ session แอดมิน: ไม่ระบุบัญชี = ล้างคุกกี้เดิมอย่างเดียว (เหมือนก่อนมีหลายบัญชี);
    ระบุบัญชี = ออกเฉพาะบัญชีนั้น ลบคุกกี้ของบัญชีนี้ และลบคุกกี้เดิมเฉพาะเมื่อเป็นของบัญชีเดียวกัน (ไม่กระทบบัญชีอื่นที่ล็อกอินอยู่)"""
    flags = cookie_security_flags(request)
    if account is not None:
        response.delete_cookie(key=admin_cookie_name(account), httponly=True, path="/", **flags)
    if account is None or _legacy_cookie_belongs_to(request, account):
        response.delete_cookie(key=ADMIN_COOKIE_NAME, httponly=True, path="/", **flags)


def _session_token(request: Request, bearer: str | None) -> tuple[str | None, str | None]:
    """หา token ของคำขอนี้ คืน (token, บัญชีที่แท็บระบุ)
    Bearer มาก่อน (client/สคริปต์เดิม) → รองลงมาคือคุกกี้ของบัญชีที่แท็บระบุ → ไม่ระบุก็ใช้คุกกี้เดิม"""
    if bearer:
        return bearer, None
    account = requested_account(request)
    if account:
        # แท็บระบุบัญชีมา → ใช้คุกกี้ของบัญชีนั้นเท่านั้น (ไม่ถอยไปใช้คุกกี้เดิม กันสวมเป็นบัญชีอื่นโดยไม่รู้ตัว)
        return request.cookies.get(admin_cookie_name(account)), account
    return request.cookies.get(ADMIN_COOKIE_NAME), None


# ตรวจสอบ Token ว่ามีหรือไม่ (Authorization: Bearer หรือคุกกี้ของบัญชี/คุกกี้ admin_token)
def verify_token(request: Request, bearer: str | None = Depends(oauth2_scheme)):
    token, account = _session_token(request, bearer)
    if not token:
        raise HTTPException(status_code=401, detail="ไม่พบ Token กรุณาเข้าสู่ระบบ")

    try:
        payload = decode_token(token)
        email   = payload.get("sub")
        user_id = payload.get("user_id")
        role    = payload.get("role")
        fullName = payload.get("FullName")

        if email is None or user_id is None or role is None or fullName is None:
            raise HTTPException(status_code=401, detail="Token ไม่ถูกต้อง")
        if account and user_id != account:
            raise HTTPException(status_code=401, detail="Token ไม่ถูกต้อง")

        # ตรวจสอบซ้ำกับฐานข้อมูลทุกครั้ง เพื่อกันกรณีบัญชี Admin ถูกลบไปแล้ว
        # แต่ Token เดิม (อายุ 24 ชม.) ยังไม่หมดอายุ
        admin = getAdminByIdDB(user_id)
        if admin is None:
            raise HTTPException(status_code=401, detail="บัญชีผู้ใช้งานนี้ถูกลบหรือไม่มีอยู่ในระบบแล้ว")

        # token ที่ออกก่อนการเปลี่ยนรหัสผ่านล่าสุดใช้ไม่ได้ (เพิกถอน session เก่าเมื่อรหัสผ่านเปลี่ยน)
        # token ที่ไม่มี iat (ออกก่อนมีระบบนี้) ยังใช้ได้จนกว่าจะมีการเปลี่ยนรหัสผ่านครั้งแรก
        changed_at = admin.get("passwordChangedAt")
        if changed_at is not None:
            issued_at = payload.get("iat")
            if not isinstance(issued_at, (int, float)) or issued_at < _to_epoch(changed_at):
                raise HTTPException(
                    status_code=401,
                    detail="Token หมดอายุเนื่องจากมีการเปลี่ยนรหัสผ่าน กรุณาเข้าสู่ระบบใหม่",
                )

        return {
            "email": email, "user_id": user_id, "role": role, "FullName" : fullName,
            "permissions": get_admin_permissions(admin),
            "level": admin.get("Level"),
        }

    except HTTPException:
        raise
    except JWTError:
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้องหรือหมดอายุ")


def require_admin(payload: dict = Depends(verify_token)):
    if payload.get("role") != "Admin":
        raise HTTPException(status_code=403, detail="หน้านี้สำหรับผู้ดูแลระบบเท่านั้น")
    return payload


def require_permission(permission: str):
    """dependency สำหรับ router/endpoint ที่ต้องมีสิทธิ์รายหน้า (ต่อจาก require_admin)"""
    def checker(payload: dict = Depends(require_admin)):
        if permission not in payload["permissions"]:
            raise HTTPException(status_code=403, detail="บัญชีนี้ไม่มีสิทธิ์ใช้งานส่วนนี้")
        return payload
    return checker


def getAdminDB(data: str) -> dict | None:
    db  = Connect_MongoDB()["BORC"]
    col = db["LoginAdmin"]
    return col.find_one({"Email": data.email})


def getAdminByIdDB(admin_id: str) -> dict | None:
    db  = Connect_MongoDB()["BORC"]
    col = db["LoginAdmin"]
    try:
        object_id = ObjectId(admin_id)
    except (InvalidId, TypeError):
        return None
    return col.find_one({"_id": object_id})


# Login มีหน้าที่ตรวจสอบ Email, Password ว่าตรงกับฐานข้อมูลหรือไม่
@router.post("/Login")
@limit("5/minute")
def login(request: Request, response: Response, data: LoginRequest):
    if not data.email or not data.password:
        raise HTTPException(status_code=400, detail="ไม่มี Username และ Password กรอกเข้ามา")

    try:
        # ตรวจสอบข้อมูล Admin ในการเข้าสู่ระบบ
        # ตอบข้อความเดียวกันทั้งกรณี "ไม่พบอีเมล" และ "รหัสผ่านผิด" (กันเดาอีเมลแอดมิน)
        # และเทียบ bcrypt เสมอ แม้ไม่พบอีเมล เพื่อให้เวลาตอบไม่ต่างกัน
        admin = getAdminDB(data)
        hashed_password = admin["Password"] if admin else _DUMMY_HASH

        password_bytes = data.password.encode("utf-8")
        # bcrypt 5.x โยน ValueError เมื่อยาวเกิน 72 ไบต์: ถือว่ารหัสผ่านผิด (401) ไม่ใช่ 500
        # แต่ยังเทียบกับ hash จริง/ปลอมด้วยค่าตัดสั้นเพื่อให้เวลาตอบใกล้เคียงกัน
        password_ok = (
            len(password_bytes) <= MAX_PASSWORD_BYTES
            and bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
        )
        if not admin or not password_ok:
            raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS_DETAIL)

        access_token = create_access_token({
            "sub"     : admin["Email"],
            "user_id" : str(admin["_id"]),
            "role"    : "Admin",
            "FullName": "ผู้ดูแลระบบ"
        })

        set_admin_cookie(request, response, access_token)
        # access_token ในตัวตอบยังคงส่งไว้ชั่วคราวเพื่อ client รุ่นเก่า (หน้าเว็บรุ่นใหม่ไม่ใช้/ไม่เก็บ ใช้คุกกี้อย่างเดียว)
        # เอาออกได้เมื่อไม่มี client รุ่นเก่าเหลือแล้ว
        return {"access_token": access_token, "user_id": str(admin["_id"])}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/Logout")
def logout(request: Request, response: Response):
    """ล้างคุกกี้ session แอดมิน (คุกกี้ httpOnly ลบจากหน้าเว็บเองไม่ได้ ต้องให้ backend สั่งลบ) — เรียกซ้ำได้ไม่ error"""
    _clear_admin_cookies(request, response, requested_account(request))
    return {"message": "ออกจากระบบสำเร็จ"}


#  ตรวจสอบว่า Login อยู่ไหม 
@router.get("/me")
def get_me(payload: dict = Depends(verify_token)):
    return {
        "FullName": payload["FullName"],
        "email"   : payload["email"],  # ให้หน้าเว็บแสดงว่าตอนนี้ล็อกอินเป็นบัญชีไหน
        "level"   : payload["level"],  # ป้ายระดับ (mid/low) ให้หน้าเว็บแสดงเป็นภาษาไทย; None = ไม่เคยระบุ
        "user_id" : payload["user_id"],
        "role"    : payload["role"],
        "permissions": payload["permissions"],
    }


# ── ROUTE: Admin Dashboard ─────────────────────────
@router.get("/admin/dashboard")
def admin_dashboard(payload: dict = Depends(verify_token)):
    return {"message": payload["FullName"]}

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

load_dotenv(override=True)


from Admin.auth.authAdmin import router as auth_router_Admin, require_admin
from Admin.router.GetProfileUser import router as GetProfile_router
from Admin.router.ManagementAccount import router as ManagementAccount_router
from Admin.router.GetHistoryAccount import router as GetHistoryAccount_router
from Admin.router.SettingAdmin import router as SettingAdmin_router
from Admin.router.test import router as CountUser_router

from users.auth.authUser import router as auth_router_user, require_advisor, require_student
from users.router.SetupProfile import router as SetupProfile_router
from users.router.WaitingApproval import router as WaitingApproval_router

from users.router.Students.BookingOnline import (
    router as BookingOnline_router,
    auto_update_status,
    ensure_booking_indexes,
)
from users.router.Students.Show_BookingData import router as ShowDataBooking_router
from users.router.Students.ManageQueueStudent import router as ManageQueueStudent_router
from users.router.Students.Reschedule_Students import router as RescheduleStudent_router
from users.router.Students.ViewConsultationHours import router as ViewConsultationHours_router
from users.router.Students.QueuehistoryStudent import router as QueuehistoryStudent_router
from users.router.Students.ChatStudent import router as ChatStudent_router


from users.router.Advisor.ManageTimeSlots import router as ManageTime_router
from users.router.Advisor.ManageQueueAdvisor import router as ManageQueueAdvisor_router
from users.router.Advisor.Rechedule_Advisor import router as RecheduleAdvisor_router
from users.router.Advisor.Show_Consult import router as ShowConsult_router
from users.router.Advisor.ConsultationAvailability import router as ConsultationAvailability_router
from users.router.Advisor.QueuehistoryAdvisor import router as QueuehistoryAdvisor_router
from users.router.Advisor.testChatAdvisor import router as DataApproved_router, client as chat_mongo_client


from users.router.SettingProfile import router as SettingProfile_router
from users.Database.ConnectDB import Connect_MongoDB


async def booking_status_worker():
    """ตรวจและอัปเดตคิว แม้ไม่มีผู้ใช้เปิดหน้าเว็บหรือเรียก API."""
    while True:
        try:
            client = Connect_MongoDB()  # shared singleton (common/mongodb_atlas.py) — ห้าม close()
            auto_update_status(client["BORC"])
        except Exception:
            logging.exception("booking status worker failed")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        client = Connect_MongoDB()  # shared singleton (common/mongodb_atlas.py) — ห้าม close()
        ensure_booking_indexes(client["BORC"])
    except Exception:
        logging.exception("booking status worker startup failed")

    worker = asyncio.create_task(booking_status_worker())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        # ปิด AsyncMongoClient ที่ chat routers (testChatAdvisor.py/ChatStudent.py) ใช้ร่วมกัน
        # ป้องกัน connection ค้างตอน shutdown (เดิมสร้างตอน import แต่ไม่เคยถูกปิด)
        await chat_mongo_client.close()


app = FastAPI(lifespan=lifespan)

# ── Rate limiting (slowapi) ────────────────────────────────────────────────
# ใช้ limiter ตัวเดียวกับที่ authAdmin.py / authUser.py ใช้ (common/rate_limit.py)
# จำกัดเฉพาะ endpoint login / OAuth code-exchange (5/minute ต่อ IP) ไม่ได้จำกัดทั้งระบบ
from common.rate_limit import limiter as _limiter

if _limiter is not None:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

load_dotenv(override=True)

# ถ้าไม่มี  production ตรงนี้จะเป็น locallhost
_ENV = os.getenv("ENV")
IS_PROD = _ENV == "production"
if _ENV is None:
    logging.warning(
        "ENV is not set — defaulting to non-production CORS behaviour "
        "(permissive localhost origins allowed). Set ENV=production explicitly in production."
    )

Backend = (os.getenv("Backend_BORC_URL") or "").strip()
Frontend = (os.getenv("Frontend_BORC_URL") or "").strip()
ChatBot = (os.getenv("ChatBot_URL") or "").strip()


# ✅ กรอง log ที่ไม่เกี่ยวข้องออก
class FilterUnwantedLog(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "/sensorsoil_api/" not in msg and "/sensorapi/" not in msg

logging.getLogger("uvicorn.access").addFilter(FilterUnwantedLog())


origins = [url for url in [Frontend, Backend, ChatBot] if url]

#ถ้าไม่มี env.IS_PROD จะเป็น localhost
if not IS_PROD:
    origins += [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:4173",
        "http://localhost:8000",
        "http://localhost:5000",
        "http://localhost:80",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Admin
app.include_router(auth_router_Admin, prefix="/authAdmin", tags=["LoginAdmin"])
admin_dependencies = [Depends(require_admin)]
app.include_router(GetProfile_router, prefix="/GETAdmin", tags=["Admin"], dependencies=admin_dependencies)
app.include_router(ManagementAccount_router, prefix="/ManagementAccount",tags=["Admin"], dependencies=admin_dependencies)
app.include_router(GetHistoryAccount_router, prefix="/HistoryAccount", tags=["Admin"], dependencies=admin_dependencies)
app.include_router(SettingAdmin_router, prefix="/settingAdmin", tags=["Admin"], dependencies=admin_dependencies)
app.include_router(CountUser_router, prefix="/Count", tags=["Admin"], dependencies=admin_dependencies)


# User ขั้นตอนการเข้าสู่ระบบ
app.include_router(auth_router_user,         prefix="/authUser", tags=["LoginUser"])
app.include_router(SetupProfile_router,      prefix="/SetupProfile", tags=["LoginUser"])
app.include_router(WaitingApproval_router,   prefix="/WaitingApproval", tags=["LoginUser"])

# User Student
student_dependencies = [Depends(require_student)]
advisor_dependencies = [Depends(require_advisor)]
app.include_router(ShowDataBooking_router,   prefix="/GETDataBookingOnline", tags=["Students"], dependencies=student_dependencies)
app.include_router(BookingOnline_router,     prefix="/booking", tags=["Students"], dependencies=student_dependencies)
app.include_router(ManageQueueStudent_router,prefix="/manage-queue", tags=["Students"], dependencies=student_dependencies)
app.include_router(RescheduleStudent_router, prefix="/reschedule", tags=["Students"], dependencies=student_dependencies)
app.include_router(ViewConsultationHours_router, prefix="/advisor-slots", tags=["Students"], dependencies=student_dependencies)
app.include_router(QueuehistoryStudent_router, prefix="/QueueHistoryStudent", tags=["Students"], dependencies=student_dependencies)


# User Advisor
app.include_router(ManageTime_router,        prefix="/ManageTimeSlots", tags=["Advisor"], dependencies=advisor_dependencies)
app.include_router(ManageQueueAdvisor_router,prefix="/advisor-queue", tags=["Advisor"], dependencies=advisor_dependencies)
app.include_router(RecheduleAdvisor_router, prefix="/advisor-reschedule", tags=["Advisor"], dependencies=advisor_dependencies)
app.include_router(ShowConsult_router, prefix="/advisor", tags=["Advisor"], dependencies=advisor_dependencies)
app.include_router(ConsultationAvailability_router ,prefix="/advisor-schedule", tags=["Advisor"], dependencies=advisor_dependencies)
app.include_router(QueuehistoryAdvisor_router ,prefix="/QueuehistoryAdvisor", tags=["Advisor"], dependencies=advisor_dependencies)

# แชท
# หมายเหตุ: handler แต่ละตัวใน router เหล่านี้เรียก verify_user_token/ensure_user_role
# (REST) หรือ get_current_user_ws/ensure_user_role (WebSocket) เองอยู่แล้วทุกตัว
# ⚠️ ห้ามใส่ dependencies=[Depends(require_student/require_advisor)] ที่ระดับ router ตรงนี้:
# require_* ไปเรียก verify_user_token(request: Request) ต่อ ซึ่ง FastAPI resolve ไม่ได้ใน
# WebSocket scope (ไม่มี Request object) ทำให้ WS route พังด้วย 500 ทันทีที่ handshake
# (เคย regression มาแล้ว — ดู tests/test_chat_routers.py สำหรับเทสต์ยืนยันพฤติกรรมนี้)
#
# ⚠️ ลำดับการ include_router มีผลจริง: ทั้งสอง router อยู่ภายใต้ prefix "/Message"
# เดียวกัน และ Starlette จะจับคู่ route ตามลำดับที่ถูก include ก่อน-หลัง
# GET /chat/history/{student_id} (advisor, path param) กับ GET /chat/history/mine
# (student, literal) มีจำนวน segment เท่ากัน — ถ้า advisor router ถูก include ก่อน
# request "/Message/chat/history/mine" จะไปแมตช์กับ advisor route ก่อนเสมอ
# (student_id="mine") แล้วโดน ensure_user_role(payload, "Advisor") ปฏิเสธด้วย 403
# ทำให้ student ไม่มีทางเรียกประวัติแชทของตัวเองได้เลย (regression จริงที่เจอจากการ
# ทดสอบผ่านหน้าเว็บจริง) จึงต้อง include ChatStudent_router (literal path) ก่อน
# DataApproved_router (path-param) เสมอ — ห้ามสลับลำดับ
app.include_router(ChatStudent_router, prefix="/Message", tags=["ChatStudent"])
app.include_router(DataApproved_router, prefix="/Message", tags=["ChatAdvisor"])


app.include_router(SettingProfile_router, prefix="/settingProfile", tags=["SettingProfile Student and Advisor"])

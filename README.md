# BORC Backend

Backend API ของระบบ BORC (Booking Online Research Consultation) — ระบบจองคิวปรึกษาอาจารย์ออนไลน์
สำหรับนักศึกษา อาจารย์ และผู้ดูแลระบบ

(ดูภาพรวมทั้งระบบ — รวม frontend — ได้ที่ `README.md` ใน root ของ repo)

## Stack

- **Framework**: FastAPI (Python 3.12)
- **Database**: MongoDB (via `pymongo`)
- **Auth**: JWT (`python-jose`) — cookie session (users) / bearer token (admin)
- **Password hashing**: `bcrypt`
- **File storage**: Cloudinary (รูปโปรไฟล์)
- **Rate limiting**: `slowapi`
- **Login ผู้ใช้ทั่วไป**: LINE Login (OAuth 2.0)
- **Server**: `uvicorn`

## โครงสร้างโปรเจกต์

```
backend/
├── main.py                     # FastAPI app, CORS, router registration, background worker
├── requirements.txt
├── .env.example
├── Dockerfile
├── common/                     # โค้ดที่ใช้ร่วมกันระหว่าง Admin และ User
│   ├── jwt_utils.py             #   encode/decode JWT กลาง (ใช้โดยทั้ง authAdmin.py และ authUser.py)
│   └── rate_limit.py            #   slowapi Limiter instance กลาง
├── Admin/
│   ├── auth/authAdmin.py        # login แอดมิน + verify_token/require_admin
│   ├── Database/ConnectDB.py    # เชื่อมต่อ MongoDB (ฝั่ง Admin)
│   ├── router/                  # endpoints จัดการบัญชีผู้ใช้/ประวัติ/ตั้งค่า
│   ├── AddLoginData.py          # สคริปต์ seed บัญชีแอดมินตัวแรก (รันตอน container start)
│   └── password_check.py
├── users/
│   ├── auth/authUser.py         # LINE login, cookie session, verify_user_token/require_student/require_advisor
│   ├── Database/ConnectDB.py    # เชื่อมต่อ MongoDB (ฝั่ง User)
│   └── router/
│       ├── Students/            # จองคิว, ประวัติ, แชท, เลื่อนนัด (มุมนักศึกษา)
│       ├── Advisor/              # จัดการช่วงเวลาว่าง, คิว, แชท (มุมอาจารย์)
│       ├── SetupProfile.py       # กรอกโปรไฟล์ครั้งแรกหลัง LINE login
│       ├── SettingProfile.py     # แก้ไขชื่อ/รูปโปรไฟล์
│       └── WaitingApproval.py    # หน้ารออนุมัติบัญชี
└── tests/                       # pytest — unit + endpoint tests
```

## ขั้นตอนการติดตั้งและรัน (local)

1. สร้าง virtualenv และติดตั้ง dependencies

   ```bash
   python -m venv env
   # Windows
   env\Scripts\activate
   # macOS/Linux
   source env/bin/activate

   pip install -r requirements.txt
   ```

2. คัดลอก `.env.example` เป็น `.env` แล้วกรอกค่าจริง (ดูรายละเอียดแต่ละตัวแปรใน `.env.example`)

   ```bash
   cp .env.example .env
   ```

3. (ครั้งแรกเท่านั้น) seed บัญชีแอดมินจาก `EMAIL_LOGIN` / `PASSWORD_LOGIN` ใน `.env`

   ```bash
   python Admin/AddLoginData.py
   ```

4. รันเซิร์ฟเวอร์

   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   API จะอยู่ที่ `http://localhost:8000` — ดู interactive docs ที่ `http://localhost:8000/docs`

## รันด้วย Docker

จาก root ของ repo (ที่มี `docker-compose.yml`) หรือ build เฉพาะ backend:

```bash
docker build -t borc-backend ./backend
docker run --env-file backend/.env -p 8000:8000 borc-backend
```

`Dockerfile` จะรัน `Admin/AddLoginData.py` (seed แอดมิน) ก่อน แล้วค่อยเริ่ม `uvicorn` — ถ้า seed
ล้มเหลว container จะหยุดทันที (กันเคส DB ยังไม่พร้อม)

## การรันชุดทดสอบ (tests)

```bash
pip install -r requirements.txt   # pytest, pytest-asyncio, mongomock รวมอยู่แล้ว
pytest
```

ชุดทดสอบใช้ `mongomock` แทน MongoDB จริง และ mock การเรียก LINE OAuth / ChatBot service ภายนอก
จึงไม่ต้องมี MongoDB หรือ service ภายนอกรันอยู่ตอนทดสอบ ดู `tests/conftest.py` สำหรับ fixtures
(เช่นการตั้งค่า environment variables จำลองก่อน import โมดูลต่าง ๆ)

## ระบบ Authentication

- **Admin**: `POST /authAdmin/Login` (email/password) → bearer JWT token, ตรวจสอบด้วย
  `OAuth2PasswordBearer` ผ่าน `Admin/auth/authAdmin.py`
- **User (Student/Advisor)**: `POST /authUser/Login` (LINE OAuth code) → JWT เก็บใน HttpOnly cookie
  ตรวจสอบผ่าน `users/auth/authUser.py`
- ทั้งสองฝั่งใช้ `common/jwt_utils.py` ร่วมกันสำหรับ encode/decode JWT (ไม่ implement ซ้ำ) แต่ยังคง
  cookie handling, role, และ business logic แยกกันตามเดิม
- Endpoint login และ OAuth code-exchange ถูกจำกัด rate ที่ 5 ครั้ง/นาที ต่อ IP ด้วย `slowapi`
  (ไม่ได้จำกัดทั้งระบบ)

## Inter-service auth: Backend → ChatBot

Backend เรียก `ChatBot_URL` (แจ้งเตือนอาจารย์/นักศึกษาเมื่อมีการจอง/ยกเลิก/เลื่อนนัด) พร้อมแนบ header
`X-Internal-Secret: <INTERNAL_SERVICE_SECRET>` ทุกครั้ง (ดูตัวแปร `CHATBOT_INTERNAL_HEADERS` ในไฟล์
router ที่เรียก ChatBot เช่น `users/router/Students/BookingOnline.py`,
`users/router/Advisor/ManageQueueAdvisor.py` เป็นต้น)

**สำคัญ**: repo นี้มีแค่ฝั่ง backend เท่านั้น — service ChatBot (ที่ให้บริการที่ `ChatBot_URL`) ต้อง
implement การตรวจสอบ header `X-Internal-Secret` เอง (เทียบกับ `INTERNAL_SERVICE_SECRET` เดียวกัน)
จึงจะปิดช่องโหว่ inter-service auth ได้สมบูรณ์ — งานนี้อยู่นอกขอบเขตของ repo นี้

## Error handling

Router ทั้งหมด: ข้อผิดพลาดที่ไม่คาดคิด (`except Exception`) จะถูก log รายละเอียดจริงด้วย
`logging` module (`logger.exception(...)`) ฝั่ง server เท่านั้น ส่วน response ที่ส่งกลับ client จะเป็น
ข้อความทั่วไป (เช่น `"Internal server error"` หรือ `"เกิดข้อผิดพลาดภายในระบบ"`) เพื่อไม่ให้ stack
trace/รายละเอียด internal หลุดไปยัง client

## MongoDB Collections (สรุปที่ backend ใช้)

`LoginAdmin`, `UserProfile`, `BookingOnline`, `ManageTimeSlots`, `QueueManagementHistory`,
`CancelBookingHistory`, `RescheduleHistory`, `AccountManagementHistory`, `ChatMessages`,
`_AutoUpdateLock`

ดูรายละเอียด schema/field แต่ละ collection ได้จาก README ใน root ของ repo

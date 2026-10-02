# BORC Backend

Backend API ของระบบ BORC (Booking Online Research Consultation) — ระบบจองคิวปรึกษาอาจารย์ออนไลน์
สำหรับนักศึกษา อาจารย์ และผู้ดูแลระบบ

(ดูภาพรวมทั้งระบบ — รวม frontend — ได้ที่ `README.md` ใน root ของ repo)

## Stack

- **Framework**: FastAPI (Python 3.12)
- **Database**: MongoDB (via `pymongo`)
- **Auth**: JWT (`python-jose`) — cookie session (users) / bearer token (admin)
- **Password hashing**: `bcrypt`
- **File storage**: MongoDB GridFS (ไฟล์แนบของคิวจอง — ไม่มี URL สาธารณะ ดาวน์โหลดผ่าน backend เฉพาะอาจารย์เจ้าของคิว) และ Cloudinary (รูปโปรไฟล์)
- **Rate limiting**: `slowapi`
- **Login ผู้ใช้ทั่วไป**: LINE Login (OAuth 2.0)
- **Server**: `uvicorn`

## โครงสร้างโปรเจกต์

```
backend/
├── main.py                     # FastAPI app, CORS, router registration, background worker
├── requirements.txt
├── Dockerfile
├── common/                     # โค้ดที่ใช้ร่วมกัน (ไม่ผูกกับ HTTP router ตัวใดตัวหนึ่ง)
│   ├── booking_status.py        #   กลุ่มสถานะคิว (ACTIVE/CANCELLABLE/CHAT ...) — แหล่งเดียว + แผนภาพวงจรชีวิตคิว
│   ├── booking_worker.py        #   worker เปลี่ยนสถานะคิวอัตโนมัติตามเวลา (Pending→Cancelled, Approved→InProgress→Completed)
│   ├── slot_service.py          #   กฎ/ตัวช่วยของ slot เวลา: cutoff, ล็อก/ปลด slot, ย้ายคิว, slot ที่จองได้
│   ├── time_slot_rules.py       #   โมเดลคำขอ + กฎเวลาของช่วงให้คำปรึกษา (ตรวจรูปแบบ, เวลาซ้อนกัน)
│   ├── indexes.py               #   สร้าง index/unique index ตอน startup
│   ├── attachments.py           #   ไฟล์แนบของคิว (GridFS): เก็บ/อ่านแบบสตรีม/ลบ, ตรวจเนื้อไฟล์
│   ├── notify.py                #   ส่งแจ้งเตือนไป ChatBot (LINE)
│   ├── jwt_utils.py             #   encode/decode JWT กลาง (ใช้โดยทั้ง authAdmin.py และ authUser.py)
│   ├── cookies.py               #   flag secure/samesite ของคุกกี้ session (ผู้ใช้และแอดมิน)
│   ├── origin_guard.py          #   ตรวจ Origin กัน CSRF / Cross-site WebSocket
│   ├── rate_limit.py            #   slowapi Limiter instance กลาง
│   ├── security_headers.py      #   security headers + ปิด /docs บน production
│   ├── chat_limits.py, url_safety.py, user_cache.py, queue_history.py, parallel.py, mongodb_atlas.py
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

2. สร้างไฟล์ `.env` ในโฟลเดอร์ `backend/` แล้วกรอกค่าจริงของตัวแปรตามหัวข้อ [ตัวแปรสภาพแวดล้อม](#ตัวแปรสภาพแวดล้อม) ด้านล่าง

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


### WebSocket ของแชท: ยืนยันตัวตนด้วย "ตั๋ว" (ไม่ใช่ cookie)
REST ของหน้าเว็บผ่าน proxy `/api` ของ Vercel (cookie session อยู่ที่โดเมนของหน้าเว็บ) แต่ WebSocket ต่อตรงไปโดเมน backend
ซึ่งเบราว์เซอร์ไม่ส่ง cookie ของโดเมนอื่นไปให้ จึงใช้ตั๋วอายุสั้น:
1. หน้าเว็บเรียก `POST /authUser/ChatTicket` (ผ่าน `/api` จึงมี cookie) ต้องมี session ที่อนุมัติแล้ว → ได้ `{ticket, expires_in: 30}`
2. เชื่อมต่อ `wss://<backend>/Message/chat/ws/{student|advisor}/<id>?ticket=<ticket>` — **ขอตั๋วใหม่ทุกครั้งที่เชื่อมต่อและต่อใหม่**
3. backend ตรวจตั๋ว (`verify_ws_ticket`): ไม่หมดอายุ, `purpose = chat_ws`, แล้วอ่านบทบาท/สถานะบัญชีจากฐานข้อมูลตอนใช้ (ไม่เชื่อค่าในตั๋ว) บัญชีที่ถูกระงับ/ลบระหว่างนั้นใช้ตั๋วไม่ได้
- ตั๋วใช้เป็น session ของ REST ไม่ได้ (และ session JWT ใช้เป็นตั๋วไม่ได้) เพราะ `verify_user_token` ปฏิเสธ token ที่มี `purpose`
- ถ้าส่ง `ticket` มาแต่ไม่ถูกต้อง จะปฏิเสธเลย (ไม่ย้อนไปใช้ cookie); ถ้าไม่ส่ง `ticket` จะใช้ cookie เหมือนเดิม (ทางสำรอง)
- ตั๋วอยู่ใน URL จึงถูกลบออกจาก access log ของ uvicorn (`common/log_redact.py`) ตั๋วอายุ 30 วินาที (`WS_TICKET_TTL` ใน `users/auth/authUser.py`)

## วันและเวลา (เวลาไทยทั้งระบบ)
- **เวลาที่เกิดเหตุการณ์** (`createdAt`, `updatedAt`, `timestamp` ฯลฯ) เก็บใน MongoDB เป็น **UTC** (`datetime.now(timezone.utc)`) — MongoDB เก็บวันเวลาเป็นช่วงเวลาสากลเสมอ
  จึงเปลี่ยนเป็น UTC+7 ในฐานข้อมูลไม่ได้และไม่ควรฝืนเก็บ "เลขนาฬิกาไทย" ลงไปเอง (เครื่องมืออื่นจะอ่านผิด 7 ชั่วโมง)
- **API ส่ง datetime ทุกตัวเป็นเวลาไทยพร้อม offset** เช่น `2026-10-02T12:48:57.574000+07:00` (จุดเวลาเดียวกับ UTC เป๊ะ) เสมอ
  (`install_thai_json_encoder()` ใน `main.py`, ตัวแปลง `to_thai_iso` อยู่ที่ `common/timefmt.py`; แชทและประวัติใช้ตัวแปลงเดียวกัน)
  เพราะ pymongo คืน datetime แบบไม่มีเขตเวลา ถ้าส่งไปตรงๆ เบราว์เซอร์จะอ่านเป็นเวลาท้องถิ่นแล้วแสดงเลื่อนไป 7 ชั่วโมง
- **หน้าเว็บแสดงเป็นเวลาไทย (Asia/Bangkok) เสมอ** ไม่ขึ้นกับเขตเวลาของเครื่องผู้ใช้ (`src/lib/format.ts`: `formatThaiDateTime`, `formatClockTime`)
  อ่านได้ทั้ง `+07:00`, `Z` และข้อความที่ไม่มีเขตเวลา (ถือเป็น UTC) ผ่าน `parseApiDateTime`
- **วัน/เวลานัดเป็นข้อความเวลาไทยอยู่แล้ว** (`Date` = `YYYY-MM-DD`, `Time` = `HH:MM-HH:MM`) ไม่ต้องแปลง; กฎทั้งหมด (จองได้ไหม, ช่วงล็อก 1 ชั่วโมง, ยกเลิก/เลื่อน)
  คำนวณด้วย `get_now_utc7()` ใน `common/booking_cutoffs.py` — ห้ามใช้ `datetime.now()`/UTC ตรงๆ เทียบกับ `Date`/`Time` ของนัด

## ตัวแปรสภาพแวดล้อม

ไม่มีไฟล์ตัวอย่าง `.env.example` (ตั้งใจให้ใช้ค่าจริง) รายการนี้สรุปจากโค้ดจริง — **ห้าม commit ไฟล์ `.env`** และไม่ใส่ค่าจริงในเอกสาร

### Backend (ตั้งใน `backend/.env` หรือใน Railway)
| ตัวแปร | จำเป็น | ใช้ทำอะไร |
|---|---|---|
| `JWT_SECRET_KEY` | **ต้องมี** (ไม่มีแอปไม่เริ่ม) | กุญแจเซ็น JWT ของ session ควรสุ่มยาวอย่างน้อย 32 ไบต์ |
| `MONGODB_ALART_CLIENT_URL` | **ต้องมี** | connection string ของ MongoDB Atlas |
| `LINE_LOGIN_CHANNEL_ID`, `LINE_LOGIN_CHANNEL_SECRET`, `LINE_LOGIN_REDIRECT_URI` | **ต้องมีทั้ง 3** (ไม่มีแอปไม่เริ่ม) | LINE Login (OAuth) `LINE_LOGIN_REDIRECT_URI` ต้องตรงกับที่ตั้งใน LINE Developers และกับ `VITE_LINE_LOGIN_REDIRECT_URI` ของ Frontend |
| `ChatBot_URL`, `INTERNAL_SERVICE_SECRET` | **ต้องมีทั้งคู่** (ไม่มีแอปไม่เริ่ม) | ปลายทางและรหัสยืนยันตัวตนตอนเรียก ChatBot (ดูหัวข้อ Inter-service auth) |
| `ENV` | แนะนำให้ตั้งเป็น `production` บน production | ถ้าไม่ตั้งจะใช้พฤติกรรมแบบ local (CORS ผ่อนปรน, ปิด `/docs` เฉพาะเมื่อเป็น production) และ **rate limit จะไม่เชื่อ header ของ proxy** (flag ของ cookie ดูจาก HTTPS ของคำขอ ไม่ได้ขึ้นกับ `ENV`) |
| `Frontend_BORC_URL`, `Backend_BORC_URL` | แนะนำ | รายชื่อ Origin ที่อนุญาต (CORS และตัวตรวจ Origin ของ POST/PUT/PATCH/DELETE/WebSocket) ถ้า `Frontend_BORC_URL` ว่าง ตัวตรวจ Origin จะไม่ทำงาน |
| `API_PROXY_SECRET` | ถ้าใช้ proxy `/api` ของ Vercel | ค่าเดียวกับตัวแปรชื่อเดียวกันใน Vercel ใช้ให้ rate limit นับตาม IP จริงของผู้ใช้ (ดูหัวข้อ Rate limit ด้านล่าง) ไม่ตั้ง = ทุกคนที่เข้าผ่าน proxy ใช้โควตา login ร่วมกัน |
| `ORIGIN_CHECK` | ไม่บังคับ (ค่าเริ่มต้น `enforce`) | `enforce` = บล็อก, `log` = บันทึกอย่างเดียว, `off` = ปิด (สวิตช์ฉุกเฉิน) |
| `ENABLE_DOCS` | ไม่บังคับ | `true` เปิด `/docs` บน production เพื่อดีบัก (ปกติปิด) |
| `CHAT_LINK_ALLOWED_HOSTS` | ไม่บังคับ | โดเมนที่อนุญาตสำหรับลิงก์นัดหมายในแชท (คั่นด้วยจุลภาค) ว่าง = ไม่จำกัดโดเมน (บังคับ https เสมอ) |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | ถ้าใช้อัปโหลดรูปโปรไฟล์ | บัญชี Cloudinary สำหรับอัปโหลดรูปโปรไฟล์ (`users/router/SettingProfile.py`) |
| `EMAIL_LOGIN`, `PASSWORD_LOGIN` | เฉพาะตอน seed แอดมินคนแรก | ใช้กับ `python Admin/AddLoginData.py` ครั้งเดียว (รหัสผ่านต้องผ่านเกณฑ์ความยาว) หลัง seed แล้วควรลบออกจาก `.env` |

### Frontend (ตั้งตอน build — ใน Vercel: Settings → Environment Variables แล้ว redeploy)
| ตัวแปร | ใช้ทำอะไร |
|---|---|
| `VITE_API_BASE_URL` | URL ของ backend (ใช้กับ WebSocket ของแชท และเป็นค่าสำรองของ REST) |
| `VITE_REST_API_BASE_URL` | บน production ตั้งเป็น `/api` ให้ REST ผ่าน proxy ของ Vercel (cookie เป็น first-party) ไม่ตั้ง = ใช้ `VITE_API_BASE_URL` |
| `VITE_LINE_LOGIN_CHANNEL_ID`, `VITE_LINE_LOGIN_REDIRECT_URI` | LINE Login ฝั่งหน้าเว็บ |
| `VITE_URL_LINEBOT` | ลิงก์เพิ่มเพื่อน LINE Bot ที่แสดงในหน้าเว็บ |

### Rate limit หลังผ่าน proxy ของ Vercel
Frontend ส่งคำขอ `/api/*` ผ่าน Vercel ไป Railway ทำให้ Railway เห็นทุกคำขอมาจาก IP ของ Vercel
เมื่อ `API_PROXY_SECRET` ตรงกันทั้งสองฝั่ง (และ `ENV=production`) backend จะเชื่อ IP แรกของ `X-Forwarded-For`
ซึ่ง Vercel เป็นผู้เขียน ผู้ใช้ปลอมไม่ได้ (ดู `common/rate_limit.py`) ถ้าไม่ตรงหรือไม่ตั้ง จะไม่เชื่อ header ใดเลย

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

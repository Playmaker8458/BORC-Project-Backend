FROM python:3.12-slim

WORKDIR /app

# ให้ /app อยู่ใน sys.path เสมอ เพื่อให้ import "common.*" หาเจอ
# แม้ตอนรัน python Admin/AddLoginData.py ตรง ๆ (ซึ่ง Python จะเติมแค่โฟลเดอร์ Admin/
# เข้า sys.path[0] ให้อัตโนมัติ ไม่ใช่ /app)
ENV PYTHONPATH=/app

# ติดตั้ง dependencies ก่อน copy โค้ดทั้งหมด
# เพื่อให้ Docker cache layer นี้ไว้ ถ้าโค้ดเปลี่ยนแต่ requirements.txt ไม่เปลี่ยน
# จะไม่ต้องติดตั้ง library ใหม่ทุกครั้ง (build เร็วขึ้นมาก)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy โค้ดทั้งหมดเข้า container
COPY . .

# รันแอปด้วยผู้ใช้ธรรมดาแทน root: ถ้าแอปถูกเจาะ ผู้โจมตีจะได้สิทธิ์จำกัด (โค้ดไม่เขียนไฟล์ลงดิสก์ จึงไม่ต้องมีสิทธิ์เขียน /app)
RUN useradd --system --no-create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# รัน AddLoginData.py ก่อน (เช่น seed ข้อมูล admin login เข้า DB)
# แล้วค่อยรัน uvicorn ต่อ ด้วย && เพื่อให้รันตามลำดับ
# ถ้า AddLoginData.py error คำสั่งจะหยุดทันที ไม่ไปรัน uvicorn ต่อ (กันเคส DB ยังไม่พร้อม)
CMD ["sh", "-c", "python Admin/AddLoginData.py && uvicorn main:app --host 0.0.0.0 --port 8000"]
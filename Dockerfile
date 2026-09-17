FROM python:3.12-slim

WORKDIR /app

# ติดตั้ง dependencies ก่อน copy โค้ดทั้งหมด
# เพื่อให้ Docker cache layer นี้ไว้ ถ้าโค้ดเปลี่ยนแต่ requirements.txt ไม่เปลี่ยน
# จะไม่ต้องติดตั้ง library ใหม่ทุกครั้ง (build เร็วขึ้นมาก)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy โค้ดทั้งหมดเข้า container
COPY . .

EXPOSE 8000

# รัน AddLoginData.py ก่อน (เช่น seed ข้อมูล admin login เข้า DB)
# แล้วค่อยรัน uvicorn ต่อ ด้วย && เพื่อให้รันตามลำดับ
# ถ้า AddLoginData.py error คำสั่งจะหยุดทันที ไม่ไปรัน uvicorn ต่อ (กันเคส DB ยังไม่พร้อม)
CMD ["sh", "-c", "python Admin/AddLoginData.py && uvicorn main:app --host 0.0.0.0 --port 8000"]
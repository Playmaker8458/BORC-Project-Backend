import logging
import os
from dotenv import load_dotenv
from Database.ConnectDB import Connect_MongoDB
from password_check import hash_password

# เข้าถึงตัวแปลในไฟล์ .env เพื่อดึงมาใช้งานในไฟล์ AddLoginData.py แบบ Local
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add_LoginData ใช้สำหรับเพิ่มข้อมูลการเข้าสู่ระบบของผู้ดูแลระบบ
def Add_LoginData(email: str, plain_password: str):
    # เข้ารหัสผ่านให้เป็นเลขฐาน 16 ด้วย bcrypt ในไฟล์ password_check 
    hashed_pw = hash_password(plain_password) 

    # MongoDB Database Connect
    myclient = Connect_MongoDB() # เชื่อมต่อ MongoDB Database
    mydb = myclient["BORC"] # เข้าถึงฐานข้อมูล BORC
    mycol = mydb["LoginAdmin"] # เข้าถึงตาราง LoginAdmin ในฐานข้อมูล BORC

    
    if mycol.find_one() == None:
        LoginData = {"Email": email, "Password": hashed_pw}
        logger.info("เพิ่มข้อมูลในตาราง LoginAdmin ในฐานข้อมูล BORC สำเร็จแล้ว")

        # เพิ่มข้อมูลที่อยู่ในตัวแปร LoginData เข้าไปเก็บในตาราง LoginAdmin ในฐานข้อมูล BORC
        mycol.insert_one(LoginData)
    else:
        logger.info("ไม่สามารถข้อมูลในตาราง LoginAdmin ได้เนื่องจากมีข้อมูลอยู่แล้ว")


# ดึงข้อมูลจากตัวแปร Email และ Password ในไฟล์ .env มาเก็บลงในตัวแปร Email และ Password
Email = os.getenv("EMAIL_LOGIN")
Password = os.getenv("PASSWORD_LOGIN")
Add_LoginData(email=Email, plain_password=Password)
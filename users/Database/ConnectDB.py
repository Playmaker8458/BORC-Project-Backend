from common.mongodb_atlas import get_atlas_client

# Connect_MongoDB ใช้สำหรับเชื่อมต่อ MongoDB Atlas ผ่าน common/mongodb_atlas.py
def Connect_MongoDB():
    return get_atlas_client()

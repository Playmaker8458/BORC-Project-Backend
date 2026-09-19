"""รันงาน I/O อิสระต่อกัน (เช่น query หลาย collection) พร้อมกันเพื่อให้รอ DB รอบเดียว

request ที่ query N ครั้งต่อกันต้องรอ N x RTT (Railway -> Atlas) การรันพร้อมกันเหลือ ~1 x RTT
MongoClient ของ pymongo รองรับหลาย thread; งานที่ส่งเข้ามาต้องไม่ส่งงานย่อยกลับเข้า pool นี้อีก
(กัน deadlock) และต้องเป็นงานอ่านที่ไม่พึ่งลำดับกัน
"""

from concurrent.futures import ThreadPoolExecutor

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-fanout")


def run_parallel(*fns):
    """เรียกทุกฟังก์ชันพร้อมกัน คืนผลตามลำดับที่ส่งเข้ามา; ถ้ามีตัวใด raise จะ raise ตัวแรกตามลำดับ"""
    futures = [_pool.submit(fn) for fn in fns]
    return [f.result() for f in futures]

"""ชั้นปีของนักศึกษา (UserProfile.YearLevel) — แหล่งเดียวของช่วงค่าที่เปิดให้เลือกและการแปลงค่า

ในฐานข้อมูลเก็บเป็น "ข้อความ" ตามค่าที่ผู้ใช้เลือกในหน้า ProfileSetup: "1" ถึง "4" และ "" เมื่อไม่ระบุ (อาจารย์/ไม่ได้เลือก)
(เหมือนฟิลด์ Role ที่ว่างเป็น "" ตอนสมัคร) แต่ข้อมูลที่แก้ใน Atlas ด้วยมืออาจเป็นตัวเลขก็ได้ จึงอ่านได้ทั้งสองแบบเสมอ
"""

# ชั้นปีที่เปิดให้เลือก — ต้องตรงกับ frontend: src/lib/constants/profile.ts: YEAR_LEVELS
YEAR_LEVELS = (1, 2, 3, 4)


def parse_year_level(value) -> int | None:
    """แปลงค่าชั้นปีที่เก็บไว้ (ตัวเลข หรือข้อความเช่น "2") เป็นตัวเลข 1-4

    ค่าอื่นทุกอย่าง (None, "", 0, 5, "abc", True, 2.5) ถือว่า "ไม่ระบุ" และคืน None
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            return None
        number = int(text)
    else:
        return None
    return number if number in YEAR_LEVELS else None


def year_level_for_storage(value: int | None) -> str:
    """ค่าที่เก็บลงฐานข้อมูล: "1"-"4" หรือ "" เมื่อไม่ระบุ"""
    return "" if value is None else str(value)

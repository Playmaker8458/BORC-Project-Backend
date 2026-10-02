"""
One-off cleanup: ล้างข้อมูลเก่าที่ค้างอยู่ของบัญชีที่ถูกลบไปแล้ว (ก่อนที่ Delete_AccountUser จะล้างข้อมูลครบ)

บัญชี "กำพร้า" (orphan) = LINE userId ที่ยังถูกอ้างถึงในข้อมูล (แชท/ประวัติ/ตำแหน่งที่อ่าน) แต่ไม่มี UserProfile แล้ว
นโยบายเดียวกับตอนลบบัญชี (common/account_cleanup.py — ใช้แผนเดียวกัน):
  - ลบ  : ข้อความแชท, ตำแหน่งที่อ่านแชท/แจ้งเตือน, แถวประวัติการจัดการคิวของบัญชีนั้นเอง
  - ถอดการผูก (แทน id ด้วย "deleted-account"): ประวัติที่อาจารย์เห็น (ApprovedHistory/RescheduleHistory/CancelBookingHistory)
    ชื่อนักศึกษาในประวัติยังอยู่ แต่บัญชีใหม่ที่ LINE userId เดิมจะไม่ได้ประวัติ/สถิติเก่ากลับมา

ค่าเริ่มต้นคือ "ดูผลอย่างเดียว" ไม่เขียนอะไร ต้องใส่ --apply จึงจะลบจริง และเมื่อ --apply:
  1. สำรองเอกสารที่จะลบ/แก้ ลงไฟล์ .jsonl ก่อน (ถ้าสำรองไม่สำเร็จ จะไม่ลบ)
  2. ต้องพิมพ์ DELETE ยืนยัน (ข้ามด้วย --yes สำหรับรันอัตโนมัติ)
  3. จำกัดจำนวนบัญชีต่อรอบ (--max-orphans, ค่าเริ่มต้น 50) กันเชื่อมผิดฐานข้อมูลแล้วลบเป็นวงกว้าง
  4. ยกเลิกทันทีถ้า UserProfile ว่าง (น่าจะเชื่อมผิดฐานข้อมูล)

Usage:
    python scripts/purge_orphaned_account_data.py                         # ดูผล (ค่าเริ่มต้น)
    python scripts/purge_orphaned_account_data.py --only-ids U123,U456     # ดูผลเฉพาะบางบัญชี
    python scripts/purge_orphaned_account_data.py --apply                  # ลบจริง (มีสำรอง + ถามยืนยัน)
    python scripts/purge_orphaned_account_data.py --report report.json     # บันทึกรายงานดูผลเป็นไฟล์

การกู้คืนจากไฟล์สำรอง: แต่ละบรรทัดเป็น JSON (bson.json_util)
  {"op": "delete", "collection": ..., "doc": <เอกสารเต็ม>}     → insert กลับด้วย doc เดิม
  {"op": "detach", "collection": ..., "_id": ..., "field": ..., "old": <ค่าเดิม>}  → ตั้งฟิลด์นั้นกลับเป็นค่าเดิม
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bson import json_util  # noqa: E402

from common.account_cleanup import DETACHED_ID, account_data_plan, purge_account_data  # noqa: E402

# ค่าพิเศษที่ไม่ใช่บัญชีผู้ใช้ (ระบบ/ค่าแทนที่ถอดการผูกแล้ว/ว่าง) — ไม่นับเป็นบัญชีกำพร้า
NOT_ACCOUNTS = {"", None, "system", DETACHED_ID}

# (คอลเลกชัน, ฟิลด์) ที่เก็บ id ผู้ใช้ — ใช้หาบัญชีกำพร้า
REFERENCES = [
    ("ChatMessages", "student_id"),
    ("ChatMessages", "advisor_id"),
    ("ChatReadState", "readerId"),
    ("ChatReadState", "counterpartId"),
    ("NotificationReadState", "userId"),
    ("QueueManagementHistory", "userId"),
    ("RescheduleHistory", "rescheduledById"),
    ("RescheduleHistory", "studentId"),
    ("RescheduleHistory", "advisorId"),
    ("CancelBookingHistory", "cancelledById"),
    ("CancelBookingHistory", "advisorId"),
    ("ApprovedHistory", "UserId"),
    ("ApprovedHistory", "AdvisorId"),
]

# ที่มาของชื่อ ไว้ช่วยให้คนดูรายงานจำได้ว่าเป็นใคร: (คอลเลกชัน, ฟิลด์ id, ฟิลด์ชื่อ)
NAME_HINTS = [
    ("CancelBookingHistory", "cancelledById", "studentName"),
    ("RescheduleHistory", "studentId", "studentName"),
    ("ApprovedHistory", "UserId", "StudentName"),
    ("RescheduleHistory", "advisorId", "advisorName"),
    ("CancelBookingHistory", "advisorId", "advisorName"),
    ("ApprovedHistory", "AdvisorId", "AdvisorName"),
]


def known_account_ids(db) -> set[str]:
    """id ที่ถือว่ายังมีบัญชีอยู่: userId ของทุกโปรไฟล์ (ทุกสถานะ) และ str(_id) ของโปรไฟล์เก่าที่ไม่มี userId
    (Delete_AccountUser ใช้ str(_id) เป็น id ของโปรไฟล์ที่ไม่มี userId)"""
    known = set()
    for doc in db["UserProfile"].find({}, {"userId": 1}):
        known.add(str(doc["_id"]))
        if doc.get("userId"):
            known.add(doc["userId"])
    return known


def find_orphans(db, only_ids: set[str] | None = None) -> dict[str, list[str]]:
    """คืน {id กำพร้า: ['คอลเลกชัน.ฟิลด์', ...]} (ต้องมีโปรไฟล์ก่อน — เรียกหลังตรวจ UserProfile ไม่ว่างแล้ว)"""
    known = known_account_ids(db)
    orphans: dict[str, set[str]] = {}
    for collection, field in REFERENCES:
        for value in db[collection].distinct(field):
            if not isinstance(value, str) or value in NOT_ACCOUNTS or value in known:
                continue
            if only_ids is not None and value not in only_ids:
                continue
            orphans.setdefault(value, set()).add(f"{collection}.{field}")
    return {k: sorted(v) for k, v in sorted(orphans.items())}


def name_hint(db, user_id: str) -> str:
    for collection, id_field, name_field in NAME_HINTS:
        doc = db[collection].find_one({id_field: user_id, name_field: {"$nin": ["", None]}}, {name_field: 1})
        if doc:
            return doc[name_field]
    return "-"


def mask(user_id: str) -> str:
    """LINE userId เป็นข้อมูลระบุตัวตน — แสดงแค่หัว/ท้ายในรายงานบนหน้าจอ (ไฟล์รายงาน/สำรองเก็บค่าเต็ม)"""
    return user_id if len(user_id) <= 10 else f"{user_id[:5]}…{user_id[-4:]}"


def build_report(db, orphans: dict[str, list[str]]) -> list[dict]:
    report = []
    for user_id, refs in orphans.items():
        report.append({
            "userId": user_id,
            "nameHint": name_hint(db, user_id),
            "referencedBy": refs,
            "wouldChange": purge_account_data(db, user_id, None, dry_run=True),
        })
    return report


def print_report(report: list[dict]) -> None:
    keys = ["chat_messages", "chat_read_state", "notification_read_state", "own_queue_history", "detached_history"]
    labels = ["แชท", "อ่านแชท", "อ่านแจ้งเตือน", "ประวัติตัวเอง", "ถอดการผูก"]
    header = f"{'บัญชี':<16}{'ชื่อ (เดาจากประวัติ)':<28}" + "".join(f"{label:>14}" for label in labels)
    print(header)
    print("-" * len(header))
    totals = dict.fromkeys(keys, 0)
    for row in report:
        for k in keys:
            totals[k] += row["wouldChange"][k]
        print(f"{mask(row['userId']):<16}{row['nameHint'][:26]:<28}" + "".join(f"{row['wouldChange'][k]:>14}" for k in keys))
    print("-" * len(header))
    print(f"{'รวม':<44}" + "".join(f"{totals[k]:>14}" for k in keys))


def write_backup(db, user_ids: list[str], directory: str) -> tuple[str, int]:
    """สำรองเอกสารที่จะลบ/แก้ ก่อนลงมือ; คืน (path, จำนวนบรรทัด) — เขียนไม่สำเร็จจะ raise (ผู้เรียกต้องไม่ลบต่อ)"""
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(directory, f"orphan_purge_backup_{stamp}.jsonl")
    lines = 0
    with open(path, "w", encoding="utf-8") as fh:
        for user_id in user_ids:
            deletes, detaches = account_data_plan(user_id, None)
            for _, collection, filt in deletes:
                for doc in db[collection].find(filt):
                    fh.write(json_util.dumps({"op": "delete", "collection": collection, "doc": doc}, ensure_ascii=False) + "\n")
                    lines += 1
            for collection, field in detaches:
                for doc in db[collection].find({field: user_id}, {field: 1}):
                    fh.write(json_util.dumps(
                        {"op": "detach", "collection": collection, "_id": doc["_id"], "field": field, "old": doc[field]},
                        ensure_ascii=False,
                    ) + "\n")
                    lines += 1
        fh.flush()
        os.fsync(fh.fileno())
    return path, lines


def main(argv=None, client=None, confirm=input) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="ลบ/ถอดการผูกจริง (ไม่ใส่ = ดูผลอย่างเดียว)")
    parser.add_argument("--yes", action="store_true", help="ข้ามการพิมพ์ DELETE ยืนยัน (ใช้กับ --apply เท่านั้น)")
    parser.add_argument("--only-ids", default="", help="จำกัดเฉพาะ userId เหล่านี้ (คั่นด้วยจุลภาค)")
    parser.add_argument("--max-orphans", type=int, default=50, help="จำนวนบัญชีสูงสุดต่อรอบเมื่อ --apply (ค่าเริ่มต้น 50)")
    parser.add_argument("--backup-dir", default="orphan_purge_backups", help="โฟลเดอร์ไฟล์สำรองเมื่อ --apply")
    parser.add_argument("--report", default="", help="บันทึกรายงานเป็นไฟล์ JSON (ค่าเต็ม ไม่ปิดบัง id)")
    args = parser.parse_args(argv)

    if client is None:
        from users.Database.ConnectDB import Connect_MongoDB
        client = Connect_MongoDB()
    db = client["BORC"]

    profile_count = db["UserProfile"].count_documents({})
    if profile_count == 0:
        print("ยกเลิก: UserProfile ว่างเปล่า — น่าจะเชื่อมต่อผิดฐานข้อมูล จึงไม่ทำอะไร")
        return 2

    only_ids = {i.strip() for i in args.only_ids.split(",") if i.strip()} or None
    orphans = find_orphans(db, only_ids)
    print(f"พบ UserProfile {profile_count} บัญชี | บัญชีกำพร้า (ยังมีข้อมูลค้างแต่ไม่มีโปรไฟล์): {len(orphans)} บัญชี\n")
    if not orphans:
        print("ไม่มีข้อมูลค้างที่ต้องล้าง")
        return 0

    report = build_report(db, orphans)
    print_report(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
        print(f"\nบันทึกรายงานที่ {args.report}")

    if not args.apply:
        print("\nนี่คือการดูผลเท่านั้น (DRY RUN) ยังไม่มีการเปลี่ยนแปลงใดๆ")
        print("ตรวจรายงานแล้วถ้าถูกต้อง ให้รันซ้ำพร้อม --apply (เลือกเฉพาะบางบัญชีได้ด้วย --only-ids)")
        return 0

    if len(orphans) > args.max_orphans:
        print(f"\nยกเลิก: พบ {len(orphans)} บัญชี เกิน --max-orphans={args.max_orphans} "
              "(ตรวจว่าเชื่อมต่อฐานข้อมูลถูกตัว หรือใช้ --only-ids ทีละกลุ่ม หรือเพิ่ม --max-orphans ถ้ามั่นใจ)")
        return 3

    if not args.yes:
        answer = confirm(f"\nจะลบ/แก้ข้อมูลของ {len(orphans)} บัญชีตามรายงานด้านบน (ย้อนกลับได้จากไฟล์สำรองเท่านั้น) พิมพ์ DELETE เพื่อยืนยัน: ")
        if answer.strip() != "DELETE":
            print("ยกเลิก: ไม่ได้ยืนยัน ไม่มีการเปลี่ยนแปลง")
            return 1

    try:
        backup_path, backup_lines = write_backup(db, list(orphans), args.backup_dir)
    except OSError as exc:
        print(f"ยกเลิก: สำรองข้อมูลไม่สำเร็จ ({exc}) จึงไม่ลบอะไร")
        return 4
    print(f"\nสำรองแล้ว {backup_lines} รายการ → {backup_path}")

    for user_id in orphans:
        done = purge_account_data(db, user_id, None)
        print(f"  {mask(user_id)}: {done}")

    remaining = find_orphans(db, set(orphans))
    if remaining:
        print(f"\nคำเตือน: ยังพบบัญชีกำพร้าค้าง {len(remaining)} บัญชี (ตรวจสอบเพิ่ม)")
        return 5
    print("\nเสร็จสิ้น: ไม่เหลือข้อมูลค้างของบัญชีที่เลือก")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
One-off migration: clear `isLocked` on slots the advisor closed manually.

SaveDaySchedule (หน้าเปิด-ปิดช่วงเวลา) used to close a slot by writing
is_closed=True AND isLocked=True with booked=0. "Locked" is meant to mean
"a student has booked it", so those slots showed as "เต็ม" instead of "ปิด" and
the advisor could no longer edit/delete them. The code now closes with
is_closed only and treats the old shape as closed (common/time_slot_rules.py:
slot_is_locked / slot_closed_by_advisor), so this script is cleanup only —
the app works the same before and after running it.

Touches only slots with booked < 1, is_closed=True, isLocked=True. Each slot is
written compare-and-set (matches the exact slot value that was read), so a
slot a student books in the meantime is left alone.

Usage:
    python scripts/normalize_closed_slots.py            # dry run (default)
    python scripts/normalize_closed_slots.py --apply     # actually write
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from users.Database.ConnectDB import Connect_MongoDB  # noqa: E402


def _is_legacy_closed(slot) -> bool:
    return (
        isinstance(slot, dict)
        and slot.get("booked", 0) < 1
        and slot.get("is_closed", False)
        and slot.get("isLocked", False)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes. Without this flag, only reports what would change.",
    )
    args = parser.parse_args()

    client = Connect_MongoDB()
    try:
        col = client["BORC"]["ManageTimeSlots"]
        found = updated = skipped = 0

        for doc in col.find({}, {"_id": 1, "advisorId": 1, "dates": 1}):
            for date, slots in (doc.get("dates") or {}).items():
                if not isinstance(slots, list):
                    continue
                for index, slot in enumerate(slots):
                    if not _is_legacy_closed(slot):
                        continue
                    found += 1
                    print(f"{doc.get('advisorId')} {date} {slot.get('start')}-{slot.get('end')}")
                    if not args.apply:
                        continue
                    result = col.update_one(
                        {"_id": doc["_id"], f"dates.{date}.{index}": slot},
                        {"$set": {f"dates.{date}.{index}.isLocked": False}},
                    )
                    if result.modified_count:
                        updated += 1
                    else:
                        skipped += 1

        if args.apply:
            print(f"\nupdated {updated} slot(s), skipped {skipped} changed since read, of {found} found")
        else:
            print(f"\n{found} slot(s) would be updated — re-run with --apply to write")
    finally:
        client.close()


if __name__ == "__main__":
    main()

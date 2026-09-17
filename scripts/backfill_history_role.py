"""
One-off migration: backfill missing `role` on old AccountManagementHistory
records written by the PATCH /GETAdmin/Profile/{user_id} bug (fixed in
Admin/router/GetProfileUser.py) where every "ยืนยันสิทธิ์แล้ว"/"แก้ไขบัญชีแล้ว"
record was saved with role="" instead of the role Admin actually picked.

AccountManagementHistory does NOT store userId (only firstName/lastName), so
this can only recover the role by matching name against the CURRENT
UserProfile — it's a best-effort approximation, not a guaranteed-accurate
historical value:
  - Skips (reports, doesn't touch) any name with zero or more-than-one match
    in UserProfile — ambiguous, can't safely guess.
  - Skips names with no UserProfile match at all (e.g. the account was later
    deleted) — nothing to backfill from.
  - For a name matched to exactly one user, every blank-role history record
    for that name gets that user's CURRENT Role. If the role was changed
    more than once after that record's action, older records will end up
    with the wrong (latest, not historical) role — accepted tradeoff since
    the original value was never captured due to the bug.

Usage:
    python scripts/backfill_history_role.py            # dry run (default)
    python scripts/backfill_history_role.py --apply     # actually write
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from users.Database.ConnectDB import Connect_MongoDB  # noqa: E402


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
        db = client["BORC"]
        col_history = db["AccountManagementHistory"]
        col_profile = db["UserProfile"]

        blank = list(col_history.find({"role": {"$in": ["", None]}}))
        if not blank:
            print("No blank-role history records found. Nothing to do.")
            return

        print(f"Found {len(blank)} history record(s) with blank role.\n")

        updated = 0
        skipped_ambiguous = 0
        skipped_no_match = 0

        for record in blank:
            first = record.get("firstName", "")
            last = record.get("lastName", "")
            matches = list(col_profile.find({"Firstname": first, "Lastname": last}))

            if len(matches) != 1:
                reason = "ambiguous (>1 match)" if matches else "no matching UserProfile"
                print(f"  SKIP  {first} {last}  [{record['_id']}]  — {reason}")
                if matches:
                    skipped_ambiguous += 1
                else:
                    skipped_no_match += 1
                continue

            current_role = matches[0].get("Role", "")
            if not current_role:
                print(f"  SKIP  {first} {last}  [{record['_id']}]  — matched user also has empty Role")
                skipped_no_match += 1
                continue

            print(f"  {'APPLY' if args.apply else 'WOULD SET'}  {first} {last}  [{record['_id']}]  role -> {current_role}")
            if args.apply:
                col_history.update_one({"_id": record["_id"]}, {"$set": {"role": current_role}})
            updated += 1

        print(
            f"\n{'Updated' if args.apply else 'Would update'}: {updated}  "
            f"Skipped (ambiguous): {skipped_ambiguous}  "
            f"Skipped (no match): {skipped_no_match}"
        )
        if not args.apply and updated:
            print("\nThis was a dry run — re-run with --apply to write changes.")
    finally:
        client.close()


if __name__ == "__main__":
    main()

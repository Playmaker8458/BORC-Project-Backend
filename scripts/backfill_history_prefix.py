"""
One-off migration: backfill missing `Prefix` on old AccountManagementHistory
records. Before the fix in Admin/router/ManagementAccount.py, "ลบบัญชีแล้ว" and
"แก้ไขบัญชีแล้ว" records written from the account-management page were saved
without a Prefix, so the history page showed "-" in the คำนำหน้า column.

AccountManagementHistory does NOT store userId (only firstName/lastName/role),
so the prefix can only be recovered by matching against the CURRENT
UserProfile — a best-effort approximation, not a guaranteed historical value:
  - Matches on firstName + lastName, and on role too when the record has one,
    to reduce ambiguity.
  - Skips (reports, doesn't touch) any record with zero or more-than-one match.
    Records for accounts that were deleted have no profile left, so their
    prefix is unrecoverable and stays blank.
  - If the prefix was changed after the record was written, the record gets the
    CURRENT prefix, not the one at the time — accepted tradeoff since the
    original value was never captured.

Usage:
    python scripts/backfill_history_prefix.py            # dry run (default)
    python scripts/backfill_history_prefix.py --apply     # actually write
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

        # $in [None] ตรงกับกรณีไม่มี field ด้วย
        blank = list(col_history.find({"Prefix": {"$in": ["", None]}}))
        if not blank:
            print("No history records with a blank Prefix found. Nothing to do.")
            return

        print(f"Found {len(blank)} history record(s) with blank Prefix.\n")

        updated = 0
        skipped_ambiguous = 0
        skipped_no_match = 0

        for record in blank:
            first = record.get("firstName", "")
            last = record.get("lastName", "")
            profile_filter = {"Firstname": first, "Lastname": last}
            if record.get("role"):
                profile_filter["Role"] = record["role"]
            matches = list(col_profile.find(profile_filter))

            if len(matches) != 1:
                reason = "ambiguous (>1 match)" if matches else "no matching UserProfile (e.g. deleted account)"
                print(f"  SKIP  {first} {last}  [{record['_id']}]  — {reason}")
                if matches:
                    skipped_ambiguous += 1
                else:
                    skipped_no_match += 1
                continue

            current_prefix = matches[0].get("Prefix", "")
            if not current_prefix:
                print(f"  SKIP  {first} {last}  [{record['_id']}]  — matched user also has empty Prefix")
                skipped_no_match += 1
                continue

            print(f"  {'APPLY' if args.apply else 'WOULD SET'}  {first} {last}  [{record['_id']}]  Prefix -> {current_prefix}")
            if args.apply:
                col_history.update_one({"_id": record["_id"]}, {"$set": {"Prefix": current_prefix}})
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

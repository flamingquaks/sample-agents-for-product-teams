"""One-time migration: seed trigger-authz grant rows from legacy capability
``authorization_users`` lists.

Before trigger authorization existed, a capability row carried an inline
``authorization_users`` list (who may @mention that agent). That field was
removed from the capability model when authorization moved to the Cedar
``SdlcTrigger`` store + data-driven ``trigger_rule`` rows. Since v2 is
unreleased there is no deployed behavior to preserve, but a fleet that was
onboarded on an earlier build of this branch may already have capability rows
carrying the old list. This migration converts each such user into a
``trigger_rule`` PERMIT row (subject=that user, agent=the capability, any
workspace) so those senders keep working the moment trigger authz is enforced.
Anything not seeded is default-deny.

Idempotent: a permit row for the same (subject, agent) is written with a
deterministic rule_id derived from the pair, so re-running overwrites rather
than duplicates. Read-only dry-run by default; pass --apply to write.

Usage:
    python scripts/migrate_authz.py --table <FleetConfigTable> --region us-west-2 [--apply]
"""

import argparse
import hashlib
import sys


def plan_migration(rows: list[dict]) -> list[dict]:
    """Pure planner (unit-tested): given the raw config-table items, return the
    list of trigger_rule permit rows to write. One rule per (agent, user) in a
    capability's legacy ``authorization_users`` list. The wildcard ``"*"`` maps to
    a permit for the wildcard subject so an open capability stays open. Skips
    empty / already-migrated entries."""
    planned: list[dict] = []
    seen: set[tuple] = set()
    for item in rows:
        if item.get("kind") != "capability":
            continue
        agent_id = item.get("agent_id")
        if not agent_id:
            continue
        for user in item.get("authorization_users", []) or []:
            subject = str(user).strip()
            if not subject:
                continue
            key = (agent_id, subject)
            if key in seen:
                continue
            seen.add(key)
            # Deterministic id so a re-run is idempotent (overwrite, not dup).
            digest = hashlib.sha256(f"{agent_id}|{subject}".encode()).hexdigest()[:24]
            planned.append(
                {
                    "pk": f"trigger_rule#migrated-{digest}",
                    "kind": "trigger_rule",
                    "rule_id": f"migrated-{digest}",
                    "connector": "slack" if subject.startswith("slack:") else "github",
                    "subject_type": "user",
                    "subject_id": subject,
                    "agent_id": agent_id,
                    "workspace": "*",
                    "effect": "permit",
                    "created_by": "migrate_authz",
                    "created_at": 0,  # stamped at write time
                }
            )
    return planned


def _scan_all(table) -> list[dict]:
    items, start_key = [], None
    while True:
        resp = table.scan(ExclusiveStartKey=start_key) if start_key else table.scan()
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", required=True, help="FleetConfig DynamoDB table name")
    ap.add_argument("--region", default=None)
    ap.add_argument("--apply", action="store_true", help="write rows (default: dry-run)")
    args = ap.parse_args(argv)

    import time

    import boto3

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    rows = _scan_all(table)
    planned = plan_migration(rows)

    if not planned:
        print("No legacy authorization_users to migrate.")
        return 0

    print(f"{'APPLYING' if args.apply else 'DRY-RUN'} — {len(planned)} permit rows:")
    for r in planned:
        print(f"  {r['agent_id']} <- {r['subject_id']} ({r['connector']})")
    if not args.apply:
        print("Re-run with --apply to write.")
        return 0

    now = int(time.time())
    for r in planned:
        r["created_at"] = now
        table.put_item(Item=r)
    print(f"Wrote {len(planned)} trigger_rule permit rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

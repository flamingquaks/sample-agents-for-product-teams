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


def _connector_for(subject: str) -> str:
    """The connector a legacy subject belongs to, from its namespace prefix, so a
    migrated rule lands under the right connector sub-page. Asana gids historically
    have no prefix; a bare numeric string is Asana, anything else defaults GitHub."""
    if subject.startswith("slack:"):
        return "slack"
    if subject.startswith("asana:") or subject.isdigit():
        return "asana"
    return "github"


def plan_migration(rows: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Pure planner (unit-tested): given the raw config-table items, return the
    list of trigger_rule permit rows to write. One rule per (agent, user) in a
    capability's legacy ``authorization_users`` list.

    The subject is NAMESPACED by source (``github:<login>`` / ``asana:<gid>``,
    Slack ids already carry ``slack:``) to match the principal the router
    authorizes on (router.namespaced_principal) and the form the dashboard rule
    editor writes — a bare id would never match and silently deny.

    The legacy wildcard ``"*"`` (allow-all) has NO equivalent in the
    principal-grant model — Cedar P1 matches a principal exactly, so a literal
    ``"*"`` subject can never match a real sender. Rather than write a dead rule
    that claims openness, we SKIP ``"*"`` and record it in ``skipped`` so the CLI
    can warn the operator to re-express that access explicitly (e.g. a group)."""
    planned: list[dict] = []
    skipped: list[tuple] = []
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
            if subject == "*":
                skipped.append((agent_id, "*"))
                continue
            connector = _connector_for(subject)
            # Namespace bare github/asana ids so they match the router principal.
            if connector in ("github", "asana") and not subject.startswith(f"{connector}:"):
                subject = f"{connector}:{subject}"
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
                    "connector": connector,
                    "subject_type": "user",
                    "subject_id": subject,
                    "agent_id": agent_id,
                    "workspace": "*",
                    "effect": "permit",
                    "created_by": "migrate_authz",
                    "created_at": 0,  # stamped at write time
                }
            )
    return planned, skipped


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
    planned, skipped = plan_migration(rows)

    if skipped:
        print(f"⚠ Skipped {len(skipped)} allow-all ('*') entries — the wildcard has")
        print("  no principal-grant equivalent. Re-express as an explicit group grant:")
        for agent_id, _ in skipped:
            print(f"    {agent_id} was open-to-all")

    if not planned:
        print("No per-user legacy authorization_users to migrate.")
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

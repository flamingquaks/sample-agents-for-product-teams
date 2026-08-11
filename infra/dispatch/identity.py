"""Cross-source identity resolver for the Dispatch Router (spec §16).

Read/enrich side of the identity map the admin API + this module both write
(schema shared with infra/dashboard/config_store.py as a contract — the packages
deploy independently, so neither imports the other; this mirrors the
fleet_config ↔ config_store and trigger_grants ↔ config_store relationships).

One record per PERSON, keyed on a synthetic ``identity_id`` (uuid). **Email is
the golden JOIN id** but is an attribute, not the key, because a first touch from
GitHub/Asana may carry no email. Every source funnels through ``resolve`` on the
way in:

  - HIT (handle already known) → return it, backfilling any newly-supplied
    email/display_name/handle additively (progressive enrichment).
  - MISS → get-or-create a NEW ``pending`` record with what we have, and the
    caller files a user-onboarding request (§16.4). A ``pending`` identity is
    known but NOT usable — the router rejects its dispatch (the onboarding gate).

The resolved identity is what makes traceability whole: the router stamps the
person's ``email`` (the long-reserved ``requester_email`` Cedar attribute) and
their ``groups`` onto the authorization call, so a grant authored against an
email or a group applies across GitHub / Asana / Slack at once.

Reads go through a short-TTL cache (mirrors trigger_grants / fleet_config) so an
admin onboarding/grouping a user propagates fleet-wide within the TTL without a
DynamoDB read per dispatch. On a cache miss the snapshot is loaded with a single
bounded Query on the ``kind-index`` GSI (``config_query.query_kind``) — not a
full-table Scan — so the load cost scales with the directory size, not the whole
fleet's config. Handle/email matches then run in memory over that snapshot.
"""

import os
import time
from dataclasses import dataclass, field

import boto3

import config_query

_IDENTITY_PK_PREFIX = "identity#"

# Mirror config_store identity constants (schema contract). `atlassian` is one
# handle key for BOTH Jira and Confluence — account ids are global across
# Atlassian products (atlassian-connector spec §A6.3).
IDENTITY_PENDING = "pending"
IDENTITY_ACTIVE = "active"
IDENTITY_DISABLED = "disabled"
IDENTITY_SOURCES = ("github", "asana", "slack", "sdlc", "atlassian")

# Short TTL so an admin onboarding/grouping a user takes effect within a minute
# across warm containers, without a DynamoDB read on every dispatch.
_CACHE_TTL_SECONDS = 30

_table = None
_cache = None  # list of identity records
_cache_expires_at = 0.0


@dataclass(frozen=True)
class Identity:
    """The resolved person for a dispatch. ``usable`` is the onboarding gate: a
    ``pending``/``disabled`` identity is known (for the reply + request) but may
    not trigger. ``created`` is True only on a first-touch create this call."""

    identity_id: str
    email: str = ""
    display_name: str = ""
    status: str = IDENTITY_PENDING
    groups: list = field(default_factory=list)
    created: bool = False

    @property
    def usable(self) -> bool:
        return self.status == IDENTITY_ACTIVE


def _get_table():
    global _table
    if _table is None:
        name = os.environ["FLEET_CONFIG_TABLE"]
        _table = boto3.resource("dynamodb").Table(name)
    return _table


def _load_identities() -> list[dict]:
    """All identity records via the kind-index (paged — a dropped record would
    fracture a person's identity into a duplicate on the next touch)."""
    return config_query.query_kind("identity")


def _snapshot(now: float | None = None) -> list[dict]:
    global _cache, _cache_expires_at
    current = time.time() if now is None else now
    if _cache is None or current >= _cache_expires_at:
        _cache = _load_identities()
        _cache_expires_at = current + _CACHE_TTL_SECONDS
    return _cache


def reset_cache() -> None:
    """Drop the cached snapshot (forces a reload on the next read). For tests."""
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


def _handle_key(source: str, handle: str, workspace: str = "") -> str:
    """Namespaced handle string — mirrors config_store._handle_key and
    router.namespaced_principal so a record's handle key equals the authz
    principal."""
    source = source.strip().lower()
    handle = handle.strip()
    if source == "slack":
        return f"slack:{workspace.strip()}:{handle}"
    return f"{source}:{handle}"


def _find_by_handle(records: list[dict], source: str, handle: str, workspace: str) -> dict | None:
    key = _handle_key(source, handle, workspace)
    for rec in records:
        if key in (rec.get("handle_keys") or []):
            return rec
    return None


def _find_by_email(records: list[dict], email: str) -> dict | None:
    norm = (email or "").strip().casefold()
    if not norm:
        return None
    for rec in records:
        if (rec.get("email") or "").strip().casefold() == norm:
            return rec
    return None


def _as_identity(rec: dict, *, created: bool = False) -> Identity:
    return Identity(
        identity_id=rec.get("identity_id", ""),
        email=rec.get("email", ""),
        display_name=rec.get("display_name", ""),
        status=rec.get("status", IDENTITY_PENDING),
        groups=list(rec.get("groups") or []),
        created=created,
    )


def _rebuild_handle_keys(handles: dict) -> list[str]:
    keys: list[str] = []
    for source, val in (handles or {}).items():
        if source == "slack" and isinstance(val, dict):
            for team, uid in val.items():
                if uid:
                    keys.append(_handle_key("slack", str(uid), str(team)))
        elif val:
            keys.append(_handle_key(source, str(val)))
    return keys


def resolve(
    *,
    source: str,
    handle: str,
    workspace: str = "",
    email: str = "",
    email_verified: bool = False,
    display_name: str = "",
) -> Identity:
    """Get-or-create + enrich the identity for a source touch. See module docstring.

    Writes go straight to DynamoDB (create on miss, additive backfill on hit) and
    invalidate the local cache so the just-written state is visible immediately.
    Never raises on a normal miss — a first-touch create is the expected path;
    the caller inspects ``.usable`` for the onboarding gate and ``.created`` /
    ``.status`` to decide whether to file/refresh a user-onboarding request.

    ``email_verified`` gates the email's identity power (§16.5, T-42). Email is
    the golden join id, so a matched email attaches the caller's handle to — or
    merges them with — the identity that already owns it, inheriting its status +
    groups. That is safe ONLY when the email came from the platform's
    authenticated directory (Slack ``users.info``, a signed GitHub/Asana event),
    NOT a user-editable profile field a caller could set to a victim's address.
    So an UNVERIFIED email is ignored entirely — never stored, never matched — and
    the default is ``False`` (fail-closed: a caller must assert verification).
    """
    if source not in IDENTITY_SOURCES or not (handle or "").strip():
        # Unresolvable — return a synthetic unusable identity so the caller
        # fails closed (never treats an unknown as usable).
        return Identity(identity_id="", status=IDENTITY_PENDING)

    records = _snapshot()
    # An unverified email carries no identity weight — drop it so it neither
    # joins the caller onto another person's record nor gets stored to be matched
    # by a later touch.
    norm_email = (email or "").strip().casefold() if email_verified else ""
    by_handle = _find_by_handle(records, source, handle, workspace)
    by_email = _find_by_email(records, norm_email) if norm_email else None

    if by_handle and by_email and by_handle["identity_id"] != by_email["identity_id"]:
        merged = _merge(keep=by_handle, drop=by_email)
        reset_cache()
        return _as_identity(merged, created=False)

    rec = by_handle or by_email
    if rec is None:
        created = _create(source, handle, workspace, norm_email, display_name)
        reset_cache()
        return _as_identity(created, created=True)

    updated = _backfill(rec, source, handle, workspace, norm_email, display_name)
    if updated is not rec:
        reset_cache()
    return _as_identity(updated, created=False)


def _create(source: str, handle: str, workspace: str, email: str, display_name: str) -> dict:
    import uuid

    identity_id = str(uuid.uuid4())
    if source == "slack":
        handles = {"slack": {workspace: handle}}
    else:
        handles = {source: handle}
    now = int(time.time())
    item = {
        "pk": f"{_IDENTITY_PK_PREFIX}{identity_id}",
        "kind": "identity",
        "identity_id": identity_id,
        "email": email,
        "display_name": display_name,
        "handles": handles,
        "handle_keys": _rebuild_handle_keys(handles),
        "groups": [],
        "verified": {},
        "status": IDENTITY_PENDING,
        "onboarded_by": "",
        "created_from": {"source": source, "handle": handle, "at": now},
        "created_at": now,
        "updated_at": now,
        "merged_from": [],
    }
    _get_table().put_item(Item=item)
    return item


def _backfill(rec: dict, source: str, handle: str, workspace: str, email: str, display_name: str) -> dict:
    """Additively enrich an existing record with any new handle/email/name.
    Returns the same object unchanged when there's nothing to add (so the caller
    can skip the cache reset)."""
    handles = dict(rec.get("handles") or {})
    changed = False
    if source == "slack":
        slack_map = dict(handles.get("slack") or {})
        if slack_map.get(workspace) != handle:
            slack_map[workspace] = handle
            handles["slack"] = slack_map
            changed = True
    elif handles.get(source) != handle:
        handles[source] = handle
        changed = True
    new_email = rec.get("email") or email
    if new_email != rec.get("email", ""):
        changed = True
    new_name = rec.get("display_name") or display_name
    if new_name != rec.get("display_name", ""):
        changed = True
    if not changed:
        return rec
    now = int(time.time())
    _get_table().update_item(
        Key={"pk": f"{_IDENTITY_PK_PREFIX}{rec['identity_id']}"},
        UpdateExpression=(
            "SET handles = :h, handle_keys = :hk, email = :e, "
            "display_name = :n, updated_at = :u"
        ),
        ExpressionAttributeValues={
            ":h": handles,
            ":hk": _rebuild_handle_keys(handles),
            ":e": new_email,
            ":n": new_name,
            ":u": now,
        },
        ConditionExpression="attribute_exists(pk)",
    )
    updated = dict(rec)
    updated.update(handles=handles, email=new_email, display_name=new_name, updated_at=now)
    return updated


def _merge(*, keep: dict, drop: dict) -> dict:
    """Fold ``drop`` into ``keep`` on an email-match collision (§16.5): union
    handles + groups, keep ``keep``'s status, keep its email or fall back to
    ``drop``'s (the survivor may be email-less), record ``merged_from``, delete
    ``drop``. This is the auto-merge on the strongest signal (email); weaker
    signals are left to admin review in the dashboard."""
    handles = dict(keep.get("handles") or {})
    for source, val in (drop.get("handles") or {}).items():
        if source == "slack" and isinstance(val, dict):
            slack_map = dict(handles.get("slack") or {})
            for team, uid in val.items():
                slack_map.setdefault(team, uid)
            handles["slack"] = slack_map
        else:
            handles.setdefault(source, val)
    groups = sorted({*keep.get("groups", []), *drop.get("groups", [])})
    verified = {**(drop.get("verified") or {}), **(keep.get("verified") or {})}
    merged_from = list(keep.get("merged_from", [])) + [drop["identity_id"]]
    # Preserve the golden join key: the survivor is the handle-matched record,
    # which may itself carry no email (GitHub-first touch). The drop was found BY
    # email, so it always has one — fall back to it rather than dropping the email
    # entirely (which would break every future find_by_email join).
    email = keep.get("email") or drop.get("email", "")
    now = int(time.time())
    _get_table().update_item(
        Key={"pk": f"{_IDENTITY_PK_PREFIX}{keep['identity_id']}"},
        UpdateExpression=(
            "SET handles = :h, handle_keys = :hk, #g = :g, verified = :v, "
            "email = :e, merged_from = :m, updated_at = :u"
        ),
        ExpressionAttributeNames={"#g": "groups"},
        ExpressionAttributeValues={
            ":h": handles,
            ":hk": _rebuild_handle_keys(handles),
            ":g": groups,
            ":v": verified,
            ":e": email,
            ":m": merged_from,
            ":u": now,
        },
        ConditionExpression="attribute_exists(pk)",
    )
    _get_table().delete_item(Key={"pk": f"{_IDENTITY_PK_PREFIX}{drop['identity_id']}"})
    survivor = dict(keep)
    survivor.update(
        handles=handles, groups=groups, verified=verified, email=email, merged_from=merged_from
    )
    return survivor


def slack_handle_for(identity_id: str, team_id: str) -> str | None:
    """This identity's Slack user id in ``team_id``, for building an @mention in
    the right workspace (§18.4). None if the identity has no slack handle there —
    the caller degrades to an unmentioned post rather than mis-pinging."""
    for rec in _snapshot():
        if rec.get("identity_id") == identity_id:
            slack = (rec.get("handles") or {}).get("slack") or {}
            return slack.get(team_id)
    return None


def find_by_handle(source: str, handle: str, workspace: str = "") -> Identity | None:
    """Look up an identity by a source handle WITHOUT creating one (read-only —
    for notification mention routing where a miss must not spawn a record)."""
    rec = _find_by_handle(_snapshot(), source, handle, workspace)
    return _as_identity(rec) if rec else None


_USER_REQUEST_PK_PREFIX = "user_req#"


def ensure_user_request(
    *,
    identity_id: str,
    source: str,
    source_context: dict | None = None,
    proposed_email: str = "",
    display_name: str = "",
) -> None:
    """File (or refresh) the ONE pending user-onboarding request for an identity
    (§16.4). Deterministic id per identity → request-once: a repeated first touch
    updates the single request instead of stacking duplicates in the admin queue
    (the REPLY, by contrast, fires every time — that's the router's job, not
    this). The dispatch package can't import the dashboard's config_store
    (separate deploy), so it writes the same ``user_request`` row shape directly,
    sharing the table as a contract (mirrors trigger_grants.put_channel_request).
    Best-effort: a write failure must not block the (already-decided) rejection."""
    if not identity_id:
        return
    request_id = f"user-{identity_id}"
    pk = f"{_USER_REQUEST_PK_PREFIX}{request_id}"
    now = int(time.time())
    try:
        table = _get_table()
        existing = table.get_item(Key={"pk": pk}).get("Item") or {}
        # Only (re)write while pending — never resurrect a decided request.
        if existing and existing.get("status") != "pending":
            return
        table.put_item(
            Item={
                "pk": pk,
                "kind": "user_request",
                "request_id": request_id,
                "identity_id": identity_id,
                "source": source,
                "source_context": source_context or {},
                "proposed_email": (proposed_email or "").strip().casefold(),
                "display_name": display_name or existing.get("display_name", ""),
                "status": "pending",
                "created_at": existing.get("created_at", now),
                "decided_by": existing.get("decided_by", ""),
                "decided_at": existing.get("decided_at"),
            }
        )
    except Exception:  # noqa: BLE001 — best-effort; rejection stands regardless
        pass

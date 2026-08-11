"""Event-automation rule engine (atlassian-connector spec §A8).

A data-driven **event → agent** engine: ``automation_rule#`` rows matched by the
receivers against normalized event facts, dispatching through the UNMODIFIED
router spine under a synthetic per-rule principal. Source-agnostic by schema
(``connector`` field) — Atlassian ships it; GitHub/Asana are a fast-follow.

Matching (§A8.2): ALL present ``match`` keys must hold (AND); ``"*"`` wildcards;
``labels_any`` is an OR-set. Called by a receiver ONLY when no mention resolved
(a mention is explicit intent and wins).

Loop safety (§A8.5): (1) the caller's bot-actor guard means an agent's own
comment/transition/page-write never reaches here; (2) a per-(rule, unit) cooldown
suppresses re-fires; (3) a per-rule hourly ceiling with a throttle metric; (4) a
chain-depth cap on automation-triggered runs.

This module reads rows through the shared config_query paged reader + a short-TTL
cache (mirrors trigger_grants), and writes cooldown/fire-count markers to the
assignments table (the TTL store the other bookkeeping rows use).
"""

import logging
import os
import re
import time

import boto3

import config_query

logger = logging.getLogger(__name__)

# Allowlisted template variables per connector (mirror config_store) — unknown
# vars render empty (§A8.1).
TEMPLATE_VARS = {
    "jira": (
        "issue_key", "summary", "project", "status", "from_status", "to_status",
        "issue_type", "reporter", "assignee", "site_url",
    ),
    "confluence": ("title", "space", "page_id", "page_url", "label", "author"),
}

AUTOMATION_MAX_FIRES_PER_HOUR = int(os.environ.get("AUTOMATION_MAX_FIRES_PER_HOUR", "20"))
_CHAIN_DEPTH_CAP = 3
_CACHE_TTL_SECONDS = 30

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "SDLCAgents/Dispatch")
STAGE = os.environ.get("STAGE", "dev")

_cache = None
_cache_expires_at = 0.0
_ddb = None
_cw = None


def _assignments_table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(
            os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments")
        )
    return _ddb


def _cloudwatch():
    global _cw
    if _cw is None:
        _cw = boto3.client("cloudwatch")
    return _cw


def reset_cache() -> None:
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


def _rules_snapshot(now: float | None = None) -> list[dict]:
    global _cache, _cache_expires_at
    current = time.time() if now is None else now
    if _cache is None or current >= _cache_expires_at:
        _cache = config_query.query_kind("automation_rule")
        _cache_expires_at = current + _CACHE_TTL_SECONDS
    return _cache


def _put_metric(name: str, dims: dict | None = None) -> None:
    d = [{"Name": "Stage", "Value": STAGE}]
    if dims:
        d.extend({"Name": k, "Value": v} for k, v in dims.items())
    try:
        _cloudwatch().put_metric_data(
            Namespace=CLOUDWATCH_NAMESPACE,
            MetricData=[{"MetricName": name, "Dimensions": d, "Value": 1.0, "Unit": "Count"}],
        )
    except Exception:  # noqa: BLE001
        logger.warning("automation: metric %s failed", name)


# --- fact normalization -------------------------------------------------------


def _event_tokens(*values: str) -> set[str]:
    """Split event-type strings into lowercase tokens for shape-agnostic matching.
    Handles BOTH the classic Jira webhook form (``jira:issue_updated``,
    ``comment_created``) and the Forge product-trigger form (``eventType`` =
    ``avi:jira:updated:issue``) by splitting on ``:`` and ``_`` — so a rule fires
    the same regardless of which transport delivered the event."""
    toks: set[str] = set()
    for v in values:
        for part in re.split(r"[:_]", (v or "").lower()):
            if part:
                toks.add(part)
    return toks


def jira_facts(event_payload: dict, site: dict) -> dict | None:
    """Normalize a Jira event payload into match facts + template values, mapping
    it to the automation event family. Robust to BOTH the classic webhook shape
    (``webhookEvent: jira:issue_updated``) and the Forge product-trigger shape
    (``eventType: avi:jira:updated:issue``) — see ``_event_tokens``. Returns None
    when the event isn't one the engine handles."""
    toks = _event_tokens(
        event_payload.get("eventType", ""), event_payload.get("webhookEvent", "")
    )
    issue = event_payload.get("issue") or {}
    fields = issue.get("fields") or {}
    key = issue.get("key", "")
    project = key.split("-", 1)[0] if "-" in key else ""
    base = {
        "site": site["site_id"],
        "site_url": site.get("site_url", ""),
        "project": project,
        "issue_key": key,
        "summary": fields.get("summary", ""),
        "status": ((fields.get("status") or {}).get("name")) or "",
        "issue_type": ((fields.get("issuetype") or {}).get("name")) or "",
        "reporter": ((fields.get("reporter") or {}).get("displayName")) or "",
        "assignee": ((fields.get("assignee") or {}).get("displayName")) or "",
        "unit": key,
    }
    # Comment first — a comment event carries "comment" in BOTH forms, and may
    # also carry "created", so it must win before the issue_created check.
    if "comment" in toks or event_payload.get("comment"):
        base["event"] = "issue_commented"
        return base
    if "updated" in toks or "generic" in toks:
        # Detect a status transition from the changelog.
        changelog = event_payload.get("changelog") or {}
        for item in (changelog.get("items") or []):
            if item.get("field") == "status":
                base["event"] = "issue_transitioned"
                base["from_status"] = item.get("fromString", "")
                base["to_status"] = item.get("toString", "")
                base["status"] = item.get("toString", "") or base["status"]
                return base
        return None  # a non-status update isn't an event family we handle
    if "created" in toks and "issue" in toks:
        base["event"] = "issue_created"
        return base
    return None


def confluence_facts(event_payload: dict, site: dict) -> dict | None:
    """Normalize a Confluence event payload into match facts + template values.
    Robust to BOTH the classic form (``event: label_added``) and the Forge
    product-trigger form (``eventType: avi:confluence:created:label``)."""
    toks = _event_tokens(
        event_payload.get("event", ""), event_payload.get("eventType", "")
    )
    page = event_payload.get("page") or event_payload.get("content") or {}
    space_key = ((page.get("space") or {}).get("key")) or event_payload.get("spaceKey", "") or ""
    page_id = str(page.get("id", ""))
    base = {
        "site": site["site_id"],
        "space": space_key,
        "page_id": page_id,
        "title": page.get("title", ""),
        "page_url": f"{site.get('site_url', '')}/wiki/spaces/{space_key}/pages/{page_id}",
        "author": ((page.get("author") or {}).get("displayName")) or "",
        "unit": page_id or space_key,
    }
    if "label" in toks:
        # Only a label ADD fires page_labeled — the manifest also forwards
        # label-removed (``avi:confluence:deleted:label``), which must NOT match.
        if "deleted" in toks or "removed" in toks:
            return None
        base["event"] = "page_labeled"
        base["label"] = event_payload.get("label", "") or (event_payload.get("labels") or [""])[0]
        base["labels_present"] = event_payload.get("labels") or ([base["label"]] if base["label"] else [])
        return base
    if "created" in toks and "page" in toks:
        base["event"] = "page_created"
        return base
    if "updated" in toks and "page" in toks:
        base["event"] = "page_updated"
        return base
    return None


# --- matching -----------------------------------------------------------------


def match(connector: str, event: str, facts: dict) -> list[dict]:
    """Enabled rules matching (connector, event, facts). ALL present match keys
    must hold (AND); ``"*"`` wildcards; ``labels_any`` is an OR-set over the
    event's present labels."""
    out = []
    for rule in _rules_snapshot():
        if not rule.get("enabled"):
            continue
        if rule.get("connector") != connector or rule.get("event") != event:
            continue
        if _rule_matches(rule.get("match") or {}, facts):
            out.append(rule)
    return out


def _rule_matches(m: dict, facts: dict) -> bool:
    for key, want in m.items():
        if key == "labels_any":
            present = set(facts.get("labels_present") or ([facts.get("label")] if facts.get("label") else []))
            if not (set(want or []) & present):
                return False
            continue
        if want in (None, "", "*"):
            continue
        if str(facts.get(key, "")) != str(want):
            return False
    return True


# --- template rendering -------------------------------------------------------


def render_template(connector: str, template: str, facts: dict) -> str:
    """Render an instruction template, substituting only allowlisted variables
    (unknown ``{{var}}`` renders empty — §A8.1). The rendered instruction passes
    the edge guardrail like any user text (T-53)."""
    allowed = set(TEMPLATE_VARS.get(connector, ()))

    def _sub(mobj):
        var = mobj.group(1).strip()
        if var in allowed:
            return str(facts.get(var, "") or "")
        return ""

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _sub, template)


# --- cooldown + ceiling + chain depth -----------------------------------------


def _cooldown_key(rule_id: str, unit: str) -> str:
    return f"auto-fire#{rule_id}#{unit}"


def _in_cooldown(rule_id: str, unit: str, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
        return False
    try:
        resp = _assignments_table().get_item(Key={"assignment_id": _cooldown_key(rule_id, unit)})
        item = resp.get("Item")
        if not item:
            return False
        return int(item.get("fired_at", 0)) + cooldown_seconds > int(time.time())
    except Exception:  # noqa: BLE001
        return False


def _record_fire(rule_id: str, unit: str, cooldown_seconds: int) -> None:
    try:
        _assignments_table().put_item(Item={
            "assignment_id": _cooldown_key(rule_id, unit),
            "kind": "automation_fire",
            "fired_at": int(time.time()),
            "ttl": int(time.time()) + max(cooldown_seconds, 3600),
        })
    except Exception:  # noqa: BLE001
        logger.exception("automation: cooldown record failed for %s/%s", rule_id, unit)


def _hourly_key(rule_id: str) -> str:
    hour = int(time.time()) // 3600
    return f"auto-hour#{rule_id}#{hour}"


def _over_hourly_ceiling(rule_id: str) -> bool:
    """Atomic per-rule hourly counter (ADD). Over the ceiling ⇒ throttle."""
    try:
        resp = _assignments_table().update_item(
            Key={"assignment_id": _hourly_key(rule_id)},
            UpdateExpression="ADD fire_count :one SET #k = :k, #t = :ttl",
            ExpressionAttributeNames={"#k": "kind", "#t": "ttl"},
            ExpressionAttributeValues={
                ":one": 1, ":k": "automation_hour", ":ttl": int(time.time()) + 7200,
            },
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp.get("Attributes", {}).get("fire_count", 0))
        return count > AUTOMATION_MAX_FIRES_PER_HOUR
    except Exception:  # noqa: BLE001
        logger.exception("automation: hourly ceiling check failed for %s", rule_id)
        return False


def match_and_dispatch(*, connector: str, event: str, facts: dict, site: dict,
                       dispatch, chain: list | None = None) -> int:
    """Match rules for this event and dispatch each through the router spine.
    ``dispatch`` is the receiver's ``_dispatch(agent_id, instruction, sender,
    context, trigger_type)`` callable. Returns the number of rules fired.

    Applies the cooldown, hourly ceiling, and chain-depth brakes (§A8.5)."""
    chain = list(chain or [])
    if len(chain) >= _CHAIN_DEPTH_CAP:
        logger.info("automation: chain depth cap reached (%s) — not matching", chain)
        return 0
    unit = str(facts.get("unit", ""))
    fired = 0
    for rule in match(connector, event, facts):
        rule_id = rule["rule_id"]
        if rule_id in chain:
            continue  # a rule may not match an event whose chain contains it
        if _in_cooldown(rule_id, unit, int(rule.get("cooldown_seconds", 3600))):
            logger.info("automation: rule %s in cooldown for unit %s", rule_id, unit)
            continue
        if _over_hourly_ceiling(rule_id):
            logger.warning("automation: rule %s over hourly ceiling — throttled", rule_id)
            _put_metric("AutomationRuleThrottled", {"RuleId": rule_id})
            continue
        action = rule.get("action") or {}
        agent_id = action.get("agent_id", "")
        instruction = render_template(connector, action.get("instruction_template", ""), facts)
        context = _dispatch_context(connector, facts, site, rule_id, chain)
        # trigger_type=automation, synthetic per-rule principal (§A8.3/§A8.4).
        dispatch(agent_id, instruction, f"automation:{connector}:{rule_id}", context, "automation")
        _record_fire(rule_id, unit, int(rule.get("cooldown_seconds", 3600)))
        _put_metric("AutomationRuleFired", {"RuleId": rule_id})
        _notify_automation_fired(connector, agent_id, facts)
        fired += 1
    return fired


def _notify_automation_fired(connector: str, agent_id: str, facts: dict) -> None:
    """Fan an ``automation_fired`` (informative) event to subscribed channels,
    scoped to the container the rule matched. Best-effort — never block dispatch."""
    try:
        import notify

        notify.notify(
            tier=notify.TIER_INFORMATIVE,
            event="automation_fired",
            text=f"🤖 Automation fired → @{agent_id} on "
                 f"{facts.get('issue_key') or facts.get('title') or facts.get('unit', '')}.",
            project=facts.get("project", "") if connector == "jira" else "",
            space=facts.get("space", "") if connector == "confluence" else "",
            unit=str(facts.get("unit", "")),
        )
    except Exception:  # noqa: BLE001
        logger.exception("automation_fired notify failed")


def _dispatch_context(connector: str, facts: dict, site: dict, rule_id: str, chain: list) -> dict:
    """Build the router dispatch context for an automation fire — the same
    source_context shape a mention dispatch carries, plus the automation trace
    ref + the chain of rule ids (chain-depth safety)."""
    import trigger_grants

    ctx = {
        "workspace": site["site_id"],
        "site_url": site.get("site_url", ""),
        "automation_rule_id": rule_id,
        "automation_chain": chain + [rule_id],
    }
    if connector == "jira":
        project_key = facts.get("project", "")
        ctx["issue_key"] = facts.get("issue_key", "")
        ctx["project_key"] = project_key
        # Linked-repo co-scope edge (§B1.2), same as a mention dispatch. The
        # first linked repo is the primary origin for the gateway header.
        repos = trigger_grants.container_repos("jira", site["site_id"], project_key)
        ctx["repos"] = repos
        ctx["repo"] = repos[0] if repos else ""
    else:
        space_key = facts.get("space", "")
        ctx["space_key"] = space_key
        ctx["page_id"] = facts.get("page_id", "")
        ctx["page_title"] = facts.get("title", "")
        row = trigger_grants.atlassian_container_row(
            "confluence", site["site_id"], space_key
        ) or {}
        ctx["write_mode"] = row.get("write_mode", "propose")
        repos = trigger_grants.container_repos("confluence", site["site_id"], space_key)
        ctx["repos"] = repos
        ctx["repo"] = repos[0] if repos else ""
    return ctx

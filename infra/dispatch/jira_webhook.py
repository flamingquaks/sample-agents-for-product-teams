"""Jira webhook receiver Lambda (atlassian-connector spec §B1.1).

Route ``POST /jira/webhook/{site}``, fed by the shared ``atlassian-events`` Forge
forwarder (§A5). Always deployed, inert until a site row enables ``jira``.

Security model (shared with the Confluence receiver):
  - Every forwarded call carries a Forge Invocation Token (FIT) — an RS256 JWT
    verified against Atlassian's JWKS (signature, expiry, audience, pinned app
    id). No shared secret exists. Bad/missing/expired ⇒ 401; JWKS unreachable ⇒
    503 (fail closed, Forge retries).
  - The installation cloud id in the token/payload is cross-checked against the
    ``{site}`` row. A mismatch ⇒ drop + metric.
  - event_id dedup (Slack-style: check-before / mark-after-success, fail-open).
  - Bot-loop guard: events authored by the fleet service account never dispatch
    or match automation rules (but agent-authored comments still feed notify).

Ack discipline: verify → dedup → async ``Event``-invoke the router → 200. The
"🏁 on it" ack is posted by the router, not here.
"""

import json
import logging
import os
import time

import boto3

import atlassian_client as ac
import automation
import mentions
import trigger_grants

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
REGISTRY_PARAM = os.environ.get("REGISTRY_PARAM", "/dispatch/agents")
STAGE = os.environ.get("STAGE", "dev")
_DEDUP_TTL_SECONDS = 24 * 60 * 60

_ssm = boto3.client("ssm")
_lambda = boto3.client("lambda")
_ddb = boto3.resource("dynamodb")

_registry = mentions.RegistryCache(REGISTRY_PARAM, lambda: _ssm)


def _assignments_table():
    return _ddb.Table(os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments"))


def _dedup_key(digest: str) -> str:
    return f"jira-event#{digest}"


def _already_seen(digest: str) -> bool:
    if not digest:
        return False
    try:
        resp = _assignments_table().get_item(Key={"assignment_id": _dedup_key(digest)})
        return "Item" in resp
    except Exception:  # noqa: BLE001
        logger.exception("jira event dedup read failed for %s; treating as new", digest)
        return False


def _mark_seen(digest: str) -> None:
    if not digest:
        return
    try:
        _assignments_table().put_item(Item={
            "assignment_id": _dedup_key(digest),
            "kind": "jira_event_dedup",
            "ttl": int(time.time()) + _DEDUP_TTL_SECONDS,
        })
    except Exception:  # noqa: BLE001
        logger.exception("jira event dedup write failed for %s", digest)


def _digest(event_type: str, issue_id: str, sub_id: str, ts: str) -> str:
    import hashlib

    raw = f"{event_type}|{issue_id}|{sub_id}|{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _dispatch(agent_id, instruction, sender, context, trigger_type):
    payload = {
        "source": "jira",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s: jira/%s", agent_id, trigger_type)
    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def _verify_and_site(event: dict, site_id: str) -> tuple[dict | None, dict | None]:
    """Verify the Forge Invocation Token and resolve the site row. Returns
    ``(site, error_response)`` — exactly one is non-None. Fail closed."""
    site = ac.get_site(site_id)
    if not site or not trigger_grants.atlassian_product_enabled(site_id, "jira"):
        logger.info("jira webhook: site %s not onboarded/jira-enabled — dropping", site_id)
        return None, {"statusCode": 200, "body": "ignored"}
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("x-forge-invocation-token") or headers.get("authorization", "").removeprefix("Bearer ")
    pinned_app_id = site.get("forge_app_id", "")
    try:
        claims = mentions.verify_forge_invocation_token(
            token,
            expected_app_id=pinned_app_id,
            # Verify the FIT audience when the deployment configures the expected
            # value (FORGE_EXPECTED_AUDIENCE — the fleet's webhook consumer ari).
            # Unset ⇒ the audience claim is not checked (the pinned app id +
            # cloud-id cross-check remain), so a mis-set value can't silently
            # deafen a site; setting it is the belt-and-braces per T-54.
            expected_audience=os.environ.get("FORGE_EXPECTED_AUDIENCE") or None,
        )
    except mentions.ForgeTokenError as exc:
        if exc.unavailable:
            logger.error("jira webhook: JWKS unavailable — 503")
            return None, {"statusCode": 503, "body": "verification unavailable"}
        logger.warning("jira webhook: FIT verification failed: %s", exc)
        return None, {"statusCode": 401, "body": "invalid token"}
    # Cross-check the installation cloud id against the site row.
    ctx = claims.get("context") or {}
    cloud_id = claims.get("cloudId") or ctx.get("cloudId") or ""
    if cloud_id and cloud_id != site_id:
        logger.warning("jira webhook: cloud id %s != site %s — dropping", cloud_id, site_id)
        return None, {"statusCode": 200, "body": "ignored"}
    # Pin the Forge app id on the first verified delivery (§A5/T-54): once set,
    # every later delivery must carry this exact app id (+ audience), so a
    # correctly-signed token from a DIFFERENT Forge app is rejected. Shared with
    # the Confluence receiver (ac.pin_forge_app_id).
    if not pinned_app_id:
        ac.pin_forge_app_id(site, claims)
    return site, None


def _stamp_liveness(site_id: str) -> None:
    try:
        _config_table().update_item(
            Key={"pk": f"atlassian_site#{site_id}"},
            UpdateExpression="SET webhook_last_seen.#p = :now",
            ExpressionAttributeNames={"#p": "jira"},
            ExpressionAttributeValues={":now": int(time.time())},
            ConditionExpression="attribute_exists(pk)",
        )
    except Exception:  # noqa: BLE001
        logger.exception("jira webhook: liveness stamp failed for %s", site_id)


_config = None


def _config_table():
    global _config
    if _config is None:
        _config = _ddb.Table(os.environ["FLEET_CONFIG_TABLE"])
    return _config


def _issue_context(site: dict, token: str, issue_key: str, comment_id: str = "") -> dict:
    """Front-load the issue snapshot + ALL comments (§B1.1), best-effort — a
    fetch failure still dispatches base context."""
    project_key = issue_key.split("-", 1)[0] if "-" in issue_key else ""
    # The project's linked-repo co-scope edge (§B1.2) — the repos an agent
    # dispatched from this project may reach with GitHub tools. Feeds the
    # dispatch block's repo lines; the first is the primary origin passed to the
    # gateway (x-dispatch-origin) so the interceptor's co-repo grouping resolves.
    repos = trigger_grants.container_repos("jira", site["site_id"], project_key)
    ctx = {
        "workspace": site["site_id"],
        "site_url": site["site_url"],
        "issue_key": issue_key,
        "project_key": project_key,
        "comment_id": comment_id,
        "repos": repos,
        "repo": repos[0] if repos else "",
    }
    try:
        status, data = ac.rest(site, "GET", f"/rest/api/3/issue/{issue_key}", token)
        if 200 <= status < 300:
            fields = data.get("fields") or {}
            ctx["issue_summary"] = fields.get("summary", "")
            ctx["issue_status"] = ((fields.get("status") or {}).get("name"))
            ctx["issue_type"] = ((fields.get("issuetype") or {}).get("name"))
            ctx["reporter_account_id"] = ((fields.get("reporter") or {}).get("accountId"))
            ctx["assignee_account_id"] = ((fields.get("assignee") or {}).get("accountId"))
        status, cdata = ac.rest(site, "GET", f"/rest/api/3/issue/{issue_key}/comment", token)
        if 200 <= status < 300:
            blocks = []
            for c in (cdata.get("comments") or []):
                author = ((c.get("author") or {}).get("displayName")) or "unknown"
                when = c.get("created", "")
                blocks.append(f"[{author} at {when}]:\n{ac.adf_to_text(c.get('body'))}")
            if blocks:
                ctx["comment_history"] = "\n\n".join(blocks)
    except Exception:  # noqa: BLE001
        logger.exception("jira webhook: context fetch failed for %s", issue_key)
    return ctx


def _process(event_payload: dict, site: dict) -> None:
    """Resolve a mention and dispatch, or match an automation rule. A mention is
    explicit intent and wins; automation runs only when no mention resolved."""
    site_id = site["site_id"]
    bot_account_id = site.get("bot_account_id", "")
    event_type = event_payload.get("webhookEvent") or event_payload.get("eventType") or ""
    issue = event_payload.get("issue") or {}
    issue_key = issue.get("key", "")
    comment = event_payload.get("comment") or {}
    author_account_id = (comment.get("author") or {}).get("accountId") or \
        (event_payload.get("user") or {}).get("accountId") or ""

    # Bot-loop guard: an event authored by the fleet service account never
    # dispatches or matches a rule.
    if bot_account_id and author_account_id == bot_account_id:
        logger.info("jira webhook: bot-authored event — no dispatch/match")
        return

    token = None
    is_comment = "comment" in event_type.lower() or bool(comment)
    if is_comment and comment:
        # Mention scan — ADF first (find the bot's mention node), text fallback.
        adf_body = comment.get("body")
        account_ids = ac.adf_mention_account_ids(adf_body)
        flat = ac.adf_to_text(adf_body)
        agent_id = None
        instruction = ""
        if bot_account_id and bot_account_id in account_ids:
            # Bot mentioned — resolve the first registry token after the mention.
            resolved = _registry.resolve_mention(flat)
            if resolved:
                agent_id, instruction = resolved
        if not agent_id:
            resolved = _registry.resolve_mention(flat)
            if resolved:
                agent_id, instruction = resolved
        if agent_id:
            token = ac.fetch_token(site)
            ctx = _issue_context(site, token, issue_key, comment.get("id", ""))
            if not instruction:
                instruction = "You were mentioned on a Jira issue. Review the thread and take appropriate action."
            _dispatch(agent_id, instruction, f"atlassian:{author_account_id}", ctx, "comment_mention")
            return

    # No mention resolved → automation matching (§A8).
    _run_automation(event_payload, site, event_type, issue_key)


def _run_automation(event_payload: dict, site: dict, event_type: str, issue_key: str) -> None:
    facts = automation.jira_facts(event_payload, site)
    if not facts:
        return
    automation.match_and_dispatch(
        connector="jira", event=facts["event"], facts=facts, site=site,
        dispatch=_dispatch,
    )


def handler(event, context=None):
    """API Gateway entry point for POST /jira/webhook/{site}."""
    path_params = event.get("pathParameters") or {}
    site_id = (path_params.get("site") or "").strip()
    site, err = _verify_and_site(event, site_id)
    if err is not None:
        return err

    _stamp_liveness(site_id)

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:  # noqa: BLE001
            return {"statusCode": 400, "body": "invalid body encoding"}
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    changelog = payload.get("changelog") or {}
    digest = _digest(
        payload.get("webhookEvent", ""),
        str(issue.get("id", "")),
        str(comment.get("id") or changelog.get("id") or ""),
        str(payload.get("timestamp", "")),
    )
    if _already_seen(digest):
        return {"statusCode": 200, "body": "duplicate"}
    try:
        _process(payload, site)
    except Exception:
        logger.exception("jira webhook: processing error")
        return {"statusCode": 500, "body": "processing error"}
    _mark_seen(digest)
    return {"statusCode": 200, "body": "ok"}

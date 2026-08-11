"""Confluence webhook receiver Lambda (atlassian-connector spec §C1.1).

Route ``POST /confluence/webhook/{site}``, fed by the shared ``atlassian-events``
Forge forwarder (§A5). Always deployed, inert until a site row enables
``confluence``. Same FIT verification / cloud-id cross-check / dedup / bot-loop
discipline as the Jira receiver (shared helpers).

Confluence specifics:
  - webhook payloads don't carry full bodies reliably: on ``comment_created`` the
    receiver fetches the comment via REST (atlas_doc_format) to scan for the bot
    mention node and resolve the agent.
  - INLINE comments carry the anchored selection
    (``inlineProperties.originalSelection``) → ``source_context.inline_selection``,
    which is what makes "update *this section*" actionable.
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
_config = None

_registry = mentions.RegistryCache(REGISTRY_PARAM, lambda: _ssm)


def _assignments_table():
    return _ddb.Table(os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments"))


def _config_table():
    global _config
    if _config is None:
        _config = _ddb.Table(os.environ["FLEET_CONFIG_TABLE"])
    return _config


def _dedup_key(digest: str) -> str:
    return f"confluence-event#{digest}"


def _already_seen(digest: str) -> bool:
    if not digest:
        return False
    try:
        return "Item" in _assignments_table().get_item(
            Key={"assignment_id": _dedup_key(digest)}
        )
    except Exception:  # noqa: BLE001
        logger.exception("confluence dedup read failed for %s; treating as new", digest)
        return False


def _mark_seen(digest: str) -> None:
    if not digest:
        return
    try:
        _assignments_table().put_item(Item={
            "assignment_id": _dedup_key(digest),
            "kind": "confluence_event_dedup",
            "ttl": int(time.time()) + _DEDUP_TTL_SECONDS,
        })
    except Exception:  # noqa: BLE001
        logger.exception("confluence dedup write failed for %s", digest)


def _digest(event: str, page_id: str, sub_id: str, ts: str) -> str:
    import hashlib

    return hashlib.sha256(f"{event}|{page_id}|{sub_id}|{ts}".encode()).hexdigest()


def _dispatch(agent_id, instruction, sender, context, trigger_type):
    payload = {
        "source": "confluence",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s: confluence/%s", agent_id, trigger_type)
    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def _verify_and_site(event: dict, site_id: str) -> tuple[dict | None, dict | None]:
    site = ac.get_site(site_id)
    if not site or not trigger_grants.atlassian_product_enabled(site_id, "confluence"):
        logger.info("confluence webhook: site %s not onboarded/confluence-enabled — dropping", site_id)
        return None, {"statusCode": 200, "body": "ignored"}
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("x-forge-invocation-token") or headers.get("authorization", "").removeprefix("Bearer ")
    pinned_app_id = site.get("forge_app_id", "")
    try:
        claims = mentions.verify_forge_invocation_token(
            token,
            expected_app_id=pinned_app_id,
            # Verify the FIT audience when the deployment configures it
            # (FORGE_EXPECTED_AUDIENCE). Unset ⇒ not checked (see jira_webhook).
            expected_audience=os.environ.get("FORGE_EXPECTED_AUDIENCE") or None,
        )
    except mentions.ForgeTokenError as exc:
        if exc.unavailable:
            return None, {"statusCode": 503, "body": "verification unavailable"}
        logger.warning("confluence webhook: FIT verification failed: %s", exc)
        return None, {"statusCode": 401, "body": "invalid token"}
    ctx = claims.get("context") or {}
    cloud_id = claims.get("cloudId") or ctx.get("cloudId") or ""
    if cloud_id and cloud_id != site_id:
        logger.warning("confluence webhook: cloud id %s != site %s — dropping", cloud_id, site_id)
        return None, {"statusCode": 200, "body": "ignored"}
    # Pin the Forge app id on the first verified delivery (§A5/T-54). Shared with
    # the Jira receiver (ac.pin_forge_app_id).
    if not pinned_app_id:
        ac.pin_forge_app_id(site, claims)
    return site, None


def _stamp_liveness(site_id: str) -> None:
    try:
        _config_table().update_item(
            Key={"pk": f"atlassian_site#{site_id}"},
            UpdateExpression="SET webhook_last_seen.#p = :now",
            ExpressionAttributeNames={"#p": "confluence"},
            ExpressionAttributeValues={":now": int(time.time())},
            ConditionExpression="attribute_exists(pk)",
        )
    except Exception:  # noqa: BLE001
        logger.exception("confluence webhook: liveness stamp failed for %s", site_id)


def _fetch_comment(site: dict, token: str, comment_id: str) -> dict:
    """Fetch a comment's ADF body + (for inline comments) the anchored selection."""
    status, data = ac.rest(
        site, "GET", f"/wiki/api/v2/comments/{comment_id}", token,
        params={"body-format": "atlas_doc_format"},
    )
    if not 200 <= status < 300:
        return {}
    return data


def _process(payload: dict, site: dict) -> None:
    site_id = site["site_id"]
    bot_account_id = site.get("bot_account_id", "")
    event = (payload.get("event") or payload.get("eventType") or "").lower()
    comment = payload.get("comment") or {}
    author_account_id = (comment.get("createdBy") or comment.get("author") or {}).get("accountId", "") or \
        (payload.get("user") or {}).get("accountId", "")

    if bot_account_id and author_account_id == bot_account_id:
        logger.info("confluence webhook: bot-authored event — no dispatch/match")
        return

    if "comment" in event and (comment or payload.get("comment")):
        comment_id = str(comment.get("id", "") or payload.get("commentId", ""))
        token = ac.fetch_token(site)
        cdata = _fetch_comment(site, token, comment_id) if comment_id else {}
        adf_raw = (((cdata.get("body") or {}).get("atlas_doc_format") or {}).get("value"))
        try:
            adf = json.loads(adf_raw) if adf_raw else None
        except (ValueError, TypeError):
            adf = None
        flat = ac.adf_to_text(adf) if adf else ""
        # An explicit agent-alias mention (@docs/@pm/…) is the trigger, mirroring
        # the Jira receiver and the Slack/GitHub alias model. The fleet bot being
        # @mentioned is NOT required — a comment naming an agent alias resolves
        # regardless. (The bot-loop guard above already drops the bot's OWN
        # comments, so this can't self-trigger.)
        resolved = _registry.resolve_mention(flat)
        agent_id, instruction = resolved if resolved else (None, "")
        if agent_id:
            page_id = str((cdata.get("pageId") or comment.get("pageId") or ""))
            ctx = _page_context(site, token, page_id, comment_id, cdata)
            if not instruction:
                instruction = "You were mentioned in a Confluence comment. Review the thread and take appropriate action."
            _dispatch(agent_id, instruction, f"atlassian:{author_account_id}", ctx, "comment_mention")
            return

    # No mention → automation matching.
    facts = automation.confluence_facts(payload, site)
    if facts:
        automation.match_and_dispatch(
            connector="confluence", event=facts["event"], facts=facts, site=site,
            dispatch=_dispatch,
        )


def _page_context(site: dict, token: str, page_id: str, comment_id: str, cdata: dict) -> dict:
    """Front-load the page snapshot + comment thread + inline selection (§C1.1),
    best-effort."""
    ctx = {
        "workspace": site["site_id"],
        "site_url": site["site_url"],
        "page_id": page_id,
        "comment_id": comment_id,
    }
    # Inline comments carry the anchored selection — the actionable "this section".
    inline = (cdata.get("resolutionStatus") is not None) or bool(cdata.get("inlineProperties"))
    sel = ((cdata.get("inlineProperties") or {}).get("originalSelection"))
    if sel:
        ctx["inline_selection"] = sel
    try:
        if page_id:
            status, data = ac.rest(
                site, "GET", f"/wiki/api/v2/pages/{page_id}", token,
                params={"body-format": "storage"},
            )
            if 200 <= status < 300:
                ctx["page_title"] = data.get("title", "")
                ctx["space_key"] = ""  # resolved below via spaceId → key
                ctx["page_version"] = (data.get("version") or {}).get("number")
                space_id = str(data.get("spaceId", ""))
                if space_id:
                    ss, sdata = ac.rest(
                        site, "GET", "/wiki/api/v2/spaces", token,
                        params={"ids": space_id, "limit": 1},
                    )
                    if 200 <= ss < 300 and (sdata.get("results") or []):
                        ctx["space_key"] = sdata["results"][0].get("key", "")
        # The space's write mode + linked-repo co-scope (§C1.2/§C3.2) — surfaced
        # in the dispatch block (propose vs direct) and the gateway origin header.
        space_key = ctx.get("space_key", "")
        if space_key:
            row = trigger_grants.atlassian_container_row(
                "confluence", site["site_id"], space_key
            ) or {}
            ctx["write_mode"] = row.get("write_mode", "propose")
            repos = trigger_grants.container_repos(
                "confluence", site["site_id"], space_key
            )
            ctx["repos"] = repos
            # First linked repo is the primary origin for the gateway header.
            ctx["repo"] = repos[0] if repos else ""
    except Exception:  # noqa: BLE001
        logger.exception("confluence webhook: page context fetch failed for %s", page_id)
    return ctx


def handler(event, context=None):
    """API Gateway entry point for POST /confluence/webhook/{site}."""
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

    comment = payload.get("comment") or {}
    page = payload.get("page") or payload.get("content") or {}
    digest = _digest(
        payload.get("event", "") or payload.get("eventType", ""),
        str(page.get("id", "")),
        str(comment.get("id") or payload.get("label") or ""),
        str(payload.get("timestamp", "")),
    )
    if _already_seen(digest):
        return {"statusCode": 200, "body": "duplicate"}
    try:
        _process(payload, site)
    except Exception:
        logger.exception("confluence webhook: processing error")
        return {"statusCode": 500, "body": "processing error"}
    _mark_seen(digest)
    return {"statusCode": 200, "body": "ok"}

"""GitHub App Webhook Receiver Lambda.

Receives webhook deliveries from the fleet's GitHub App and forwards recognized
@agent mentions to the Dispatch Router — the server-side replacement for the old
`.github/workflows/agent-dispatch.yml`, which required a GitHub Actions workflow
(and an OIDC deploy role) in every onboarded repo. With the GitHub App, one
webhook endpoint receives events for every repo the App is installed on, so
onboarding a repo needs no per-repo workflow and no repo-side AWS credential.

Sits behind API Gateway (public HTTPS). Security model mirrors the Asana receiver
(asana_webhook.py):
  - Every delivery is authenticated by its HMAC-SHA256 signature
    (``X-Hub-Signature-256: sha256=<hex>``) against the App's webhook secret,
    stored as an SSM SecureString. Missing/empty secret hard-fails closed.
  - The webhook secret is fetched per-invocation and held only in local scope,
    never a module global (threat T-8).
  - Full issue/PR context is fetched server-side with a per-repo, least-privilege
    GitHub App installation token (github_app.installation_token_for_repo) —
    replacing the workflow's ``gh api`` calls that ran with the Actions token.

Handled events: ``issue_comment`` (created) and ``pull_request_review_comment``
(created) — a comment must @mention a known agent, else no-op 200. Additionally
``pull_request`` (opened / closed-merged / review_requested) and ``issues``
(opened) are fanned out to subscribed Slack channels as **SCM notifications**
(spec §18.3) — these never dispatch an agent; they only notify.
"""

import base64
import json
import logging
import os
import time

import boto3

import automation
import mentions
import notify

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")
# The App webhook secret — an SSM SecureString, populated when the App is
# registered (github_client.exchange_manifest_code writes it during the admin
# manifest flow). Fetched per invocation, never cached in a module global.
GITHUB_WEBHOOK_SECRET_PARAM = os.environ.get(
    "GITHUB_WEBHOOK_SECRET_PARAM", "/sdlc-agents/github-webhook-secret"
)
# The Dispatch Router registry (rendered from the active capability rows and
# written to SSM by the dashboard on every capability change). This receiver
# resolves @mentions against it so a UI-onboarded agent becomes reachable from
# GitHub with NO code change here — the same registry the router resolves
# against. Cached with a short TTL so an onboard/disable propagates within the
# window across warm invocations.
REGISTRY_PARAM = os.environ.get("REGISTRY_PARAM", "/dispatch/agents")
# The GitHub App slug — its bot login is ``<slug>[bot]``. Used by the
# automation bot-actor guard so an auto-review never fires on the fleet's own
# PR activity (loop safety, reviewer-agent spec §auto-trigger step 2). Read
# per-invocation with a short-TTL cache; absent ⇒ the guard simply can't match
# the bot login (fail-open on the guard is acceptable — the cooldown + chain
# brakes still bound loops; but the slug is normally set once the App exists).
GITHUB_APP_SLUG_PARAM = os.environ.get("GITHUB_APP_SLUG_PARAM")

_ssm = boto3.client("ssm")
_lambda = boto3.client("lambda")

_slug_cache: dict = {"value": None, "expires_at": 0.0}
_SLUG_TTL = 300.0

# Sentinel prefixes meaning "no App slug yet". infra/foundation/template.yaml
# seeds the slug param with Value: "unset"; older docs/deploys used
# "placeholder". The check must cover BOTH: a wrong sentinel silently builds a
# bot login (e.g. "unset[bot]") that matches nothing, gutting the exact-match
# half of the bot-actor guard. Matched case-insensitively as prefixes.
_NO_SLUG_SENTINELS = ("unset", "placeholder")

# Per-repo cache of the .pdlc-agents/review.yaml `review_drafts` opt-in, short
# TTL (mirrors _slug_cache) — a push storm on a draft PR must not hammer the
# GitHub contents API with one config fetch per delivery.
_drafts_cfg_cache: dict = {}
_DRAFTS_CFG_TTL = 300.0

# Registry-backed @mention resolution + a short-TTL cache, shared with the other
# receivers (mentions.py). The registry — not a hardcoded roster — decides which
# agents are reachable, so a UI-onboarded agent resolves here with NO code change.
_registry = mentions.RegistryCache(REGISTRY_PARAM, lambda: _ssm)


def _get_secret() -> str | None:
    try:
        resp = _ssm.get_parameter(Name=GITHUB_WEBHOOK_SECRET_PARAM, WithDecryption=True)
    except _ssm.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"] or None


def _verify_signature(secret: str, raw_body: str, signature_header: str) -> bool:
    """Constant-time check of the ``sha256=<hex>`` GitHub signature over the EXACT
    raw request body (the caller must pass the unparsed body)."""
    return mentions.verify_hmac_sha256(secret, raw_body, signature_header, prefix="sha256=")


def _resolve_agent(body: str) -> str | None:
    return _registry.resolve_agent(body)


def _dispatch(agent_id: str, instruction: str, sender: str, context: dict, trigger_type: str):
    payload = {
        "source": "github",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s: github/%s", agent_id, trigger_type)
    _lambda.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",  # async — don't block the webhook response
        Payload=json.dumps(payload).encode(),
    )


def _issue_context(repo: str, issue_number: int) -> dict:
    """Fetch full issue/PR context server-side with a per-repo App token — the
    context the router + agent need, matching what agent-dispatch.yml assembled
    via ``gh api``. Best-effort: if enrichment fails, dispatch still proceeds with
    the base context so a transient GitHub error doesn't drop the mention."""
    ctx = {"repo": repo, "issue_number": str(issue_number)}
    try:
        import github_app
        import requests

        token = github_app.installation_token_for_repo(repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        base = f"{github_app.GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
        issue = requests.get(base, headers=headers, timeout=10).json()
        comments = requests.get(f"{base}/comments", headers=headers, timeout=10).json()
        ctx.update(
            {
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body") or "",
                "issue_labels": ", ".join(l.get("name", "") for l in issue.get("labels", [])),
                "issue_assignees": ", ".join(a.get("login", "") for a in issue.get("assignees", [])),
                "issue_state": issue.get("state", ""),
                "is_pr": "true" if issue.get("pull_request") else "false",
                "issue_comments": "\n".join(
                    f"[{c.get('user', {}).get('login', '?')} at {c.get('created_at', '')}]:\n{c.get('body', '')}\n"
                    for c in (comments if isinstance(comments, list) else [])
                ),
            }
        )
    except Exception:  # noqa: BLE001
        # Never let enrichment failure drop the dispatch; log without echoing
        # any token/PII, and proceed with the base context.
        logger.exception("issue context enrichment failed for %s#%s", repo, issue_number)
    return ctx


def _process_comment(payload: dict, trigger_type: str):
    """Handle an issue_comment / pull_request_review_comment 'created' event."""
    if payload.get("action") != "created":
        return
    comment = payload.get("comment", {})
    body = comment.get("body", "")
    agent_id = _resolve_agent(body)
    if not agent_id:
        return  # no known agent mentioned

    # sender = the commenter's GitHub LOGIN (stable, used by the router's
    # authorization allowlist). GitHub logins are unique + not self-editable to
    # an arbitrary existing login, so they're a safe principal (parallels the
    # Asana receiver keying on the immutable user gid).
    sender = comment.get("user", {}).get("login", "")
    repo = payload.get("repository", {}).get("full_name", "")
    issue = payload.get("issue") or payload.get("pull_request") or {}
    issue_number = issue.get("number")
    if not repo or issue_number is None:
        logger.warning("comment event missing repo/issue number; skipping")
        return

    instruction = body.strip()
    context = _issue_context(repo, issue_number)
    _dispatch(agent_id, instruction, sender, context, trigger_type)


# Event type (X-GitHub-Event header) → processor trigger_type.
_EVENT_TRIGGER = {
    "issue_comment": "comment_mention",
    "pull_request_review_comment": "pr_comment",
}


def _notify_scm(event_type: str, payload: dict) -> None:
    """Fan a GitHub SCM event out to subscribed Slack channels (spec §18.3). These
    are NOT agent dispatches — they only notify channels that opted into the
    matching tier/event for the repo. Best-effort: a notification failure never
    fails the webhook. The @mention actor is the PR/issue author, resolved to the
    right Slack user per workspace by the identity map (informative tier for
    open/merge, so no mention fires; kept for parity + future actionable events)."""
    action = payload.get("action", "")
    repo = (payload.get("repository") or {}).get("full_name", "")
    if not repo:
        return

    # (event, action) → (tier, notify-event id, text builder). Only the
    # transitions a channel can subscribe to in the modal are mapped.
    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        num = pr.get("number")
        title = pr.get("title", "")
        author = (pr.get("user") or {}).get("login", "")
        unit = f"pr:{repo}:{num}"
        actor = {"source": "github", "handle": author, "workspace": ""}
        if action == "opened":
            notify.notify(
                tier=notify.TIER_INFORMATIVE, event="pr_opened",
                text=f"📥 PR opened in `{repo}` #{num}: {title}",
                repo=repo, unit=unit, actor=actor,
            )
        elif action == "closed" and pr.get("merged"):
            notify.notify(
                tier=notify.TIER_INFORMATIVE, event="pr_merged",
                text=f"✅ PR merged in `{repo}` #{num}: {title}",
                repo=repo, unit=unit, actor=actor,
            )
        elif action == "review_requested":
            # Actionable — a review is being asked for. Mention the reviewer.
            reviewer = (payload.get("requested_reviewer") or {}).get("login", "")
            notify.notify(
                tier=notify.TIER_ACTIONABLE, event="review_requested",
                text=f"👀 Review requested on `{repo}` #{num}: {title}",
                repo=repo, unit=unit,
                actor={"source": "github", "handle": reviewer, "workspace": ""},
            )
    elif event_type == "issues" and action == "opened":
        issue = payload.get("issue") or {}
        num = issue.get("number")
        title = issue.get("title", "")
        author = (issue.get("user") or {}).get("login", "")
        notify.notify(
            tier=notify.TIER_INFORMATIVE, event="issue_opened",
            text=f"🐛 Issue opened in `{repo}` #{num}: {title}",
            repo=repo, unit=f"issue:{repo}:{num}",
            actor={"source": "github", "handle": author, "workspace": ""},
        )


def _app_bot_login() -> str:
    """The fleet App's bot login (``<slug>[bot]``), short-TTL cached. Empty when
    the slug param is unset/unreadable — the caller then can't match the bot and
    the cooldown/chain brakes remain the loop backstop."""
    if not GITHUB_APP_SLUG_PARAM:
        return ""
    now = time.time()
    if _slug_cache["value"] is not None and now < _slug_cache["expires_at"]:
        return _slug_cache["value"]
    slug = ""
    try:
        resp = _ssm.get_parameter(Name=GITHUB_APP_SLUG_PARAM)
        raw = (resp["Parameter"]["Value"] or "").strip()
        # The template seeds a sentinel before the App exists — treat any
        # sentinel value as "no slug" so we never build a bogus bot login
        # (see _NO_SLUG_SENTINELS for why both prefixes must be covered).
        if raw and not raw.lower().startswith(_NO_SLUG_SENTINELS):
            slug = f"{raw}[bot]"
    except Exception:  # noqa: BLE001
        logger.warning("could not read GitHub App slug param %s", GITHUB_APP_SLUG_PARAM)
    _slug_cache["value"] = slug
    _slug_cache["expires_at"] = now + _SLUG_TTL
    return slug


def _repo_reviews_drafts(repo: str) -> bool:
    """Whether ``repo`` opted into auto-reviewing draft PRs via the
    ``review_drafts`` key of ``.pdlc-agents/review.yaml`` on its default branch
    (reviewer-agent spec §Repo config). Best-effort trigger-layer read: EVERY
    failure path — missing file/404, bad YAML, token error, network error —
    returns False, because the safe default is skip-drafts and a config fetch
    failure must never block the webhook. Cached per-repo with a short TTL
    (_drafts_cfg_cache) so a push storm on a draft PR doesn't hammer the
    contents API."""
    now = time.time()
    cached = _drafts_cfg_cache.get(repo)
    if cached and now < cached["expires_at"]:
        return cached["value"]
    value = False
    try:
        import github_app
        import requests
        import yaml

        token = github_app.installation_token_for_repo(repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.get(
            f"{github_app.GITHUB_API_BASE}/repos/{repo}/contents/.pdlc-agents/review.yaml",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            raw = base64.b64decode((resp.json().get("content") or "").encode()).decode("utf-8")
            config = yaml.safe_load(raw)
            if isinstance(config, dict):
                value = bool(config.get("review_drafts", False))
    except Exception:  # noqa: BLE001 — fail closed to the skip-drafts default
        logger.warning("could not read .pdlc-agents/review.yaml for %s", repo)
    _drafts_cfg_cache[repo] = {"value": value, "expires_at": now + _DRAFTS_CFG_TTL}
    return value


def _run_automation(payload: dict) -> None:
    """Match a pull_request event against GitHub automation rules and dispatch
    (reviewer-agent spec §auto-trigger). Runs ONLY after _notify_scm and ONLY
    when the PR author is not the fleet bot (loop guard) — a mention is explicit
    intent and takes its own path; automation is the no-mention fallback.
    Best-effort: a matching/dispatch failure never fails the webhook."""
    pr = payload.get("pull_request") or {}
    author = (pr.get("user") or {}).get("login", "")
    # The event actor — on `synchronize` this is the PUSHER, which may be the
    # fleet bot pushing to a human-opened PR (durable repo work pushes to
    # wip/... with the App token). The PR author stays the human in that case,
    # so guarding on the author alone would let a bot push re-trigger a review
    # (a loop the head-SHA ledger dampens but doesn't structurally prevent).
    sender = (payload.get("sender") or {}).get("login", "")
    bot_login = _app_bot_login()
    # Bot-actor guard: the App's own PR activity never triggers an auto-review.
    # Match either the PR author OR the delivery sender against the fleet bot
    # login; also skip any *[bot] actor as a coarse backstop when the slug is
    # unknown.
    for actor in (author, sender):
        if actor and (actor == bot_login or actor.endswith("[bot]")):
            logger.info("github webhook: bot actor (%s) — no automation", actor)
            return
    # Draft PRs are not auto-reviewed by default (reviewer-agent spec §Non-goals:
    # "Review draft PRs unless the repo config opts in"). A draft is work in
    # progress — reviewing it wastes a run and posts premature findings/status.
    # The per-repo `review_drafts` opt-in (.pdlc-agents/review.yaml, spec §Repo
    # config) is honored HERE at the trigger layer — the agent-side config read
    # happens post-dispatch, too late to matter for this skip. Only consulted
    # when the PR is actually a draft (never an API call for non-draft PRs),
    # fail-closed to skip. (An @reviewer mention still reviews a draft — that's
    # explicit human intent on its own path.)
    if pr.get("draft"):
        repo = (payload.get("repository") or {}).get("full_name", "")
        if not _repo_reviews_drafts(repo):
            logger.info("github webhook: draft PR — no auto-review")
            return
    facts = automation.github_facts(payload)
    if not facts:
        return  # not an opened/synchronize event we auto-review
    automation.match_and_dispatch(
        connector="github", event=facts["event"], facts=facts,
        dispatch=_dispatch,
    )


def handler(event, context=None):
    """API Gateway entry point for GitHub App webhook deliveries."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""
    # API Gateway base64-encodes the body when the route matches a binary media
    # type. GitHub signs the EXACT decoded bytes, so decode BEFORE computing the
    # HMAC or every affected delivery fails verification.
    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("could not base64-decode webhook body")
            return {"statusCode": 400, "body": "invalid body encoding"}

    # --- authenticate the delivery ---
    secret = _get_secret()
    if not secret:
        logger.error(
            "GitHub webhook secret %s not set — refusing events. Register the "
            "GitHub App in the dashboard admin UI first.",
            GITHUB_WEBHOOK_SECRET_PARAM,
        )
        return {"statusCode": 503, "body": "webhook not configured"}
    signature = headers.get("x-hub-signature-256", "")
    if not _verify_signature(secret, raw_body, signature):
        logger.warning("Invalid GitHub webhook signature")
        return {"statusCode": 401, "body": "invalid signature"}

    event_type = headers.get("x-github-event", "")
    if event_type == "ping":
        # GitHub sends a ping on webhook creation — acknowledge it.
        return {"statusCode": 200, "body": "pong"}

    trigger_type = _EVENT_TRIGGER.get(event_type)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    # SCM notification events (pull_request / issues) don't dispatch an agent —
    # they fan out to subscribed Slack channels (spec §18.3). A comment event
    # can ALSO be worth notifying, but today only the mention→dispatch path runs
    # for comments; SCM notifications cover the non-comment lifecycle.
    if event_type in ("pull_request", "issues"):
        try:
            _notify_scm(event_type, payload)
        except Exception:  # noqa: BLE001 — notifications are best-effort
            logger.exception("SCM notification fan-out failed for %s", event_type)
        # Auto-review: after notifying, match pull_request events against GitHub
        # automation rules (reviewer-agent spec §auto-trigger). No rule / no
        # match / bot author ⇒ no-op. Never fails the webhook.
        if event_type == "pull_request":
            try:
                _run_automation(payload)
            except Exception:  # noqa: BLE001 — automation is best-effort here
                logger.exception("GitHub automation dispatch failed")
        return {"statusCode": 200, "body": "ok"}

    if not trigger_type:
        # A subscribed event we neither route nor notify (push, etc.) — ack.
        return {"statusCode": 200, "body": "ignored"}

    try:
        _process_comment(payload, trigger_type)
    except Exception:  # noqa: BLE001
        logger.exception("error processing GitHub %s event", event_type)
        return {"statusCode": 500, "body": "processing error"}
    return {"statusCode": 200, "body": "ok"}

"""Dispatch Router Lambda.

Single entry point for all agent work assignments. Receives normalized events
from GitHub Actions, Asana webhook receiver, and Slack, then:

1. Resolves @mention → agent ID (including aliases)
2. Checks authorization
3. Checks concurrency limits
4. Records assignment in DynamoDB
5. Invokes AgentCore Runtime
6. Returns acknowledgment to caller

The router is intentionally simple — all intelligence lives in the agents.
Adding a new agent is a self-service onboard in the Admin dashboard (which builds
its container, stands up its runtime, and republishes this registry), not a code
change.
"""

import hashlib
import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal

import boto3
import enrichment
import fleet_config
import guardrail
import identity as identity_map
import notify
import reply
import trigger_authz
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Clients -----------------------------------------------------------------

dynamodb = boto3.resource("dynamodb")
assignments_table = dynamodb.Table(
    os.environ.get("ASSIGNMENTS_TABLE", "dispatch-assignments")
)
ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")
agentcore = boto3.client(
    "bedrock-agentcore",
    config=Config(read_timeout=900, connect_timeout=10, retries={"max_attempts": 0}),
)

CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "SDLCAgents/Dispatch")
STAGE = os.environ.get("STAGE", "dev")

BLOCKED_MESSAGE_TEMPLATE = (
    "This request was blocked by a prompt-injection safety filter"
    "{reason_suffix}. No agent was invoked. If you believe this is a "
    "false positive, an operator can inspect assignment `{assignment_id}` "
    "in CloudWatch and re-dispatch manually."
)

GUARDRAIL_ERROR_MESSAGE_TEMPLATE = (
    "The prompt-injection safety check is temporarily unavailable; your "
    "request was not dispatched. Please try again in a few minutes. "
    "Assignment `{assignment_id}`."
)

# --- Agent Registry ----------------------------------------------------------
# Loaded from SSM Parameter Store (rendered from the capability rows by the
# dashboard admin API / capability deployer on every capability change).
#
# Cached with a SHORT TTL, not for the whole execution-environment lifetime: the
# registry is now runtime-mutable (an admin onboard/enable/disable republishes it
# immediately), so a lifetime cache on a warm Lambda would keep resolving to a
# just-disabled agent — or miss a just-onboarded one — until a cold start. The
# TTL bounds that staleness to _REGISTRY_TTL_SECONDS fleet-wide (mirrors
# fleet_config's short-TTL cache, which replaced this same lifetime-cache
# pattern).

_registry_cache = None
_registry_expires_at = 0.0
_REGISTRY_TTL_SECONDS = 30


def load_registry():
    """Load the agent registry from SSM Parameter Store, refreshing when the TTL
    has expired. On a refresh error, keeps serving the last-known-good cache if we
    have one (availability > freshness for an already-loaded registry)."""
    global _registry_cache, _registry_expires_at
    now = time.time()
    if _registry_cache is not None and now < _registry_expires_at:
        return _registry_cache

    try:
        param = ssm.get_parameter(
            Name=os.environ.get("REGISTRY_PARAM", "/dispatch/agents"),
            WithDecryption=False,
        )
        import yaml

        _registry_cache = yaml.safe_load(param["Parameter"]["Value"])
        _registry_expires_at = now + _REGISTRY_TTL_SECONDS
    except Exception:
        if _registry_cache is not None:
            logger.exception("registry refresh failed; serving stale cache")
            return _registry_cache
        raise
    return _registry_cache


# --- Mention Parsing ---------------------------------------------------------

MENTION_PATTERN = re.compile(
    r"@(\w+)",
    re.IGNORECASE,
)


def resolve_agent(mention: str, registry: dict) -> dict | None:
    """Resolve a mention string to an agent config, checking aliases."""
    mention = mention.lower()
    agents = registry.get("agents", {})

    # Direct match
    if mention in agents:
        return {**agents[mention], "agent_id": mention}

    # Alias match
    for agent_id, config in agents.items():
        if mention in config.get("aliases", []):
            return {**config, "agent_id": agent_id}

    return None


def extract_mention_and_instruction(
    body: str, registry: dict
) -> tuple[dict | None, str]:
    """Extract the first recognized @agent mention and the instruction text."""
    for match in MENTION_PATTERN.finditer(body):
        agent = resolve_agent(match.group(1), registry)
        if agent is not None:
            instruction = body[match.end() :].strip()
            return agent, instruction
    return None, ""


# --- Authorization -----------------------------------------------------------


_UNRESOLVED_SENDERS = {"", "unknown"}


def namespaced_principal(sender: str, source: str) -> str:
    """The authorization principal for a raw ``sender``, namespaced by source.

    The receivers emit source-native ids (GitHub login, Asana gid), and Slack
    already emits ``slack:<team>:<uid>``. We prefix github/asana here — in the
    ONE place every source funnels through — so a principal is globally
    unambiguous (an Asana gid can't collide with a GitHub login) and matches the
    ``github:<login>`` / ``asana:<gid>`` form the dashboard rule editor writes. A
    sender already carrying its ``<source>:`` prefix (Slack, or a re-dispatch) is
    left as-is.

    Both Atlassian products map to ONE principal namespace —
    ``atlassian:<accountId>`` — because account ids are global across Jira and
    Confluence (atlassian-connector spec §A6.3); the dispatch ``source`` still
    distinguishes the products.

    A synthetic automation principal (``automation:<connector>:<rule_id>``,
    §A8.4) is ALREADY fully namespaced in its own reserved namespace — never a
    person — so it is returned verbatim, not re-prefixed with the source (which
    would produce ``atlassian:automation:…`` and never match the rule's grant)."""
    if not sender:
        return sender
    if is_synthetic_principal(sender):
        return sender
    if source in ("jira", "confluence") and not sender.startswith("atlassian:"):
        return f"atlassian:{sender}"
    if source in ("github", "asana") and not sender.startswith(f"{source}:"):
        return f"{source}:{sender}"
    return sender


def is_synthetic_principal(sender: str) -> bool:
    """Whether ``sender`` is a synthetic (non-person) principal — today the
    automation engine's per-rule principal (``automation:<connector>:<rule_id>``,
    §A8.4). These are authorized through the same AVP path as people (via an
    auto-authored grant) but must skip person identity resolution + the
    first-touch onboarding gate — there is no human behind them to onboard."""
    return bool(sender) and sender.startswith("automation:")


def _identity_source_and_handle(sender: str, source: str, source_context: dict) -> tuple[str, str, str]:
    """Split a namespaced principal into (identity_source, handle, workspace) for
    the identity resolver. Slack senders are ``slack:<team>:<uid>``; github/asana
    are the bare login/gid (namespaced only for authz). Jira/Confluence senders
    resolve to the ONE ``atlassian`` identity source (account ids are global
    across products, §A6.3). Returns the identity ``source`` (one of
    IDENTITY_SOURCES), the source-native handle, and the workspace ("" for
    sources without one)."""
    if source == "slack" and sender.startswith("slack:"):
        parts = sender.split(":", 2)
        if len(parts) == 3:
            return "slack", parts[2], parts[1]
    if source in ("jira", "confluence"):
        handle = sender.split(":", 1)[1] if sender.startswith("atlassian:") else sender
        return "atlassian", handle, str(source_context.get("workspace", "") or "")
    return source, sender, str(source_context.get("workspace", "") or "")


def resolve_dispatch_identity(sender: str, source: str, source_context: dict):
    """Resolve (get-or-create + enrich) the person behind a dispatch. Returns an
    identity_map.Identity. The resolved email + groups feed authorization
    (traceability spine, §16.7).

    A receiver may seed ``requester_email`` ONLY from the platform's
    authenticated directory (e.g. Slack ``users.info``, a signed GitHub/Asana
    event) and MUST then set ``requester_email_verified`` — email is the golden
    join id, so an unverified address a caller could forge must not attach them
    to another person's identity (T-42). No receiver seeds it today; the resolver
    fails closed on the missing flag regardless."""
    id_source, handle, workspace = _identity_source_and_handle(sender, source, source_context)
    return identity_map.resolve(
        source=id_source,
        handle=handle,
        workspace=workspace,
        email=str(source_context.get("requester_email", "") or ""),
        email_verified=bool(source_context.get("requester_email_verified", False)),
        display_name=str(source_context.get("sender_name", "") or ""),
    )


def onboarding_reply(source: str, source_context: dict, org_owned: bool) -> str:
    """The reply for a not-onboarded / pending sender (§16.4). Org repos resolve
    the member's email so we can promise an email on completion; personal repos
    cannot, so the copy just points at the admin."""
    if org_owned:
        return (
            "A request to onboard you has been sent to the SDLC admin and you'll "
            "receive an email when onboarding is complete. If you have any "
            "questions, please contact your SDLC Admin. Once onboarded, please "
            "try your request again."
        )
    return (
        "You're not onboarded to the SDLC fleet. Please contact your SDLC Admin "
        "for onboarding."
    )


def _is_org_owned(source: str, source_context: dict) -> bool:
    """Whether this dispatch originates from an org-owned GitHub repo (drives the
    onboarding reply copy). Only GitHub has the owner concept; other sources use
    the email-promise copy since Slack/Asana yield an email at first touch."""
    if source != "github":
        return True
    repo = str(source_context.get("repo", "") or "")
    owner = repo.split("/", 1)[0] if "/" in repo else ""
    return fleet_config.owner_type(owner) == "Organization"


def authorize_trigger(
    agent_config: dict, sender: str, source: str, source_context: dict | None = None
) -> tuple[bool, str]:
    """Authorize a trigger, returning ``(allowed, reason)``.

    Trigger authorization is decided ONLY by the Cedar ``SdlcTrigger`` policy
    store (``trigger_authz.is_authorized``), keyed on the sender, the agent, and
    the request's source/workspace/channel context. There is no per-capability
    allowlist — every source (GitHub, Asana, Slack) authorizes through this one
    path, with the source namespaced into the principal.

    Fail-closed on an unresolved sender ("" / "unknown") BEFORE calling AVP —
    these sentinels mean the upstream receiver couldn't resolve a stable
    identity, and allowing them would turn a misconfigured policy into a
    universal bypass (threat T-4). We never pass such a sentinel to AVP as a
    principal. ``reason`` names why a denial happened, for the reject notice +
    metrics."""
    agent_id = agent_config.get("agent_id", "?")
    if not sender or sender in _UNRESOLVED_SENDERS:
        logger.warning(
            "Agent '%s' received an unresolved sender (%r); rejecting.",
            agent_id,
            sender,
        )
        return False, "unresolved-sender"

    decision = trigger_authz.is_authorized(
        principal=namespaced_principal(sender, source),
        agent_id=agent_id,
        source=source,
        context=source_context or {},
    )
    return decision.allow, ("" if decision.allow else decision.reason)


def check_authorization(
    agent_config: dict, sender: str, source: str, source_context: dict | None = None
) -> bool:
    """Boolean trigger-authz check (thin wrapper over ``authorize_trigger``).

    Retained as the primary predicate the handler calls; use ``authorize_trigger``
    directly when the denial ``reason`` is needed (e.g. the in-thread reject
    notice)."""
    allowed, _ = authorize_trigger(agent_config, sender, source, source_context)
    return allowed


# --- Repo binding (multi-repo allowlist) -------------------------------------


def check_repo_allowed(source: str, source_context: dict) -> bool:
    """Confirm a repo-carrying dispatch targets repos the fleet (and, for Slack,
    the CHANNEL) has approved.

    - GitHub: the origin repo must be onboarded (and, when the fleet is
      restricted, multi-repo eligible) per the runtime fleet config.
    - Slack: a dispatch that names repos (``/sdlc-message-agent``) may only name
      repos APPROVED FOR ITS CHANNEL (spec §19) — the admin grants that set when
      approving the channel. The modal only offers approved repos and the webhook
      re-checks on submit; this is the fail-closed backstop at the router (the
      single choke point every trigger passes). Grouped siblings of an approved
      repo remain reachable by the AGENT via co-repo mechanics — but can't be
      named as the dispatch target from the channel. A Slack dispatch with no
      repos passes (agent-only / Asana work).
    - Other sources carry no repo and always pass.

    The allowlist is read from the fleet-config table through a short-TTL cache,
    so an admin change takes effect fleet-wide within the cache window. Fails
    closed: an empty/not-yet-allowed repo is rejected rather than let through.
    """
    if source == "github":
        repo = str(source_context.get("repo", "")).strip()
        return fleet_config.is_repo_allowed(repo)
    if source == "slack":
        repos = [str(r).strip().casefold() for r in (source_context.get("repos") or []) if r]
        single = str(source_context.get("repo", "")).strip().casefold()
        if single and single not in repos:
            repos.append(single)
        if not repos:
            return True  # agent-only work — no repo named
        import trigger_grants

        workspace = str(source_context.get("workspace", "")).strip()
        channel = str(source_context.get("channel_id", "")).strip()
        approved = {r.casefold() for r in trigger_grants.channel_repos(workspace, channel)}
        return all(
            r in approved and fleet_config.is_repo_allowed(r) for r in repos
        )
    return True


# --- Concurrency -------------------------------------------------------------


def check_concurrency(agent_id: str, max_concurrent: int) -> bool:
    """Check if the agent has capacity for another assignment."""
    response = assignments_table.query(
        IndexName="agent_id-status-index",
        KeyConditionExpression="agent_id = :aid AND #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":aid": agent_id,
            ":status": "dispatched",
        },
    )
    active_count = response.get("Count", 0)
    return active_count < max_concurrent


# --- Assignment Tracking -----------------------------------------------------


def create_assignment(
    agent_id: str,
    source: str,
    trigger_type: str,
    requester: str,
    instruction: str,
    source_context: dict,
    parent_assignment_id: str = "",
) -> str:
    """Record a new assignment in DynamoDB. Returns assignment_id.

    ``parent_assignment_id`` links a reply-on-a-completed-thread follow-up to
    the prior assignment (durable-repo-work spec D8) — a NEW unit of work, not
    a reopen."""
    assignment_id = str(uuid.uuid4())
    now = int(time.time())
    ttl = now + (30 * 24 * 60 * 60)  # 30 days

    # Enrich the record with the structured, traceable dimensions the dashboard
    # renders. Derived purely from data we already have in-hand — no new
    # external collection. ``trace_refs`` is an open map (repo/branch/pr/issue/
    # jira/asana…); agents may add more at completion via update_trace_refs.
    trace_refs = enrichment.derive_trace_refs(source, source_context, instruction)
    participants = enrichment.derive_participants(source, requester, source_context)

    item_extra = (
        {"parent_assignment_id": parent_assignment_id} if parent_assignment_id else {}
    )
    assignments_table.put_item(
        Item={
            **item_extra,
            "assignment_id": assignment_id,
            # Turn-by-turn record of the run (the dashboard's conversation
            # view). Append-only: the agent adds question/result events, the
            # router adds reply events on resume — each atomically with the
            # status write that produces it.
            "timeline": [
                {
                    "ts": now,
                    "kind": "dispatched",
                    "actor": requester,
                    "text": instruction[:2000],
                }
            ],
            # Constant PK for the "all runs, newest-first" fleet GSI (AllRunsIndex).
            "gsi_all": enrichment.ALL_RUNS_PK,
            "agent_id": agent_id,
            "source": source,
            "trigger_type": trigger_type,
            "requester": requester,
            "instruction": instruction,
            "status": "dispatched",
            "source_context": source_context,
            "trace_refs": trace_refs,
            "participants": participants,
            "created_at": now,
            "completed_at": None,
            "duration_seconds": None,
            "result_summary": None,
            # token_usage / cost_estimate_usd are deliberately ABSENT (not
            # None): the agent accumulates them with DynamoDB ADD on every
            # segment of a (possibly resumed) run, and ADD rejects a NULL-typed
            # attribute — an explicit None here would break usage capture.
            "ttl": ttl,
        }
    )
    return assignment_id


# --- Thread ↔ assignment binding (durable-repo-work spec) ---------------------
#
# A Slack dispatch binds its thread to the assignment so a later in-thread
# @sdlc-agents reply can resume a paused run (or start a linked follow-up on a
# completed one) WITHOUT naming the agent — the binding resolves it. Mirror of
# the notif_thread# bookkeeping rows, same table.

_THREAD_BINDING_PREFIX = "thread_binding#"
_BINDING_TTL_SECONDS = 30 * 24 * 60 * 60


def thread_binding_key(team_id: str, channel_id: str, thread_ts: str) -> str:
    return f"{_THREAD_BINDING_PREFIX}{team_id}#{channel_id}#{thread_ts}"


def write_thread_binding(source_context: dict, assignment_id: str, agent_id: str) -> None:
    """Bind the dispatch's Slack thread to this assignment (best-effort — a
    missed binding only costs resumability, never the dispatch).

    A binding whose CURRENT assignment is paused (``awaiting_input``) or
    mid-resume is NOT overwritten: the pause message told the user "reply in
    this thread to resume", and rebinding the thread to a newer dispatch would
    permanently orphan the paused run (its reply would route to the new
    assignment). The new dispatch still proceeds — it just doesn't steal the
    thread's resume anchor while a pause is outstanding."""
    team = str(source_context.get("workspace", "") or "")
    channel = str(source_context.get("channel_id", "") or "")
    thread_ts = str(source_context.get("thread_ts", "") or "")
    if not (team and channel and thread_ts):
        return
    key = thread_binding_key(team, channel, thread_ts)
    try:
        existing = assignments_table.get_item(Key={"assignment_id": key}).get("Item") or {}
        bound_id = str(existing.get("bound_assignment_id", "") or "")
        if bound_id and bound_id != assignment_id:
            bound = (
                assignments_table.get_item(
                    Key={"assignment_id": bound_id},
                    ProjectionExpression="#s",
                    ExpressionAttributeNames={"#s": "status"},
                ).get("Item")
                or {}
            )
            if bound.get("status") in ("awaiting_input", "resuming"):
                logger.info(
                    "thread binding kept on paused assignment %s (not rebound to %s)",
                    bound_id,
                    assignment_id,
                )
                return
        assignments_table.put_item(
            Item={
                "assignment_id": key,
                "kind": "thread_binding",
                "bound_assignment_id": assignment_id,
                "agent_id": agent_id,
                "ttl": int(time.time()) + _BINDING_TTL_SECONDS,
            }
        )
    except Exception:
        logger.exception("thread binding write failed for %s", assignment_id)


def update_assignment(assignment_id: str, **kwargs):
    """Update fields on an existing assignment."""
    update_parts = []
    attr_names = {}
    attr_values = {}

    for key, value in kwargs.items():
        placeholder = f"#{key}"
        value_placeholder = f":{key}"
        update_parts.append(f"{placeholder} = {value_placeholder}")
        attr_names[placeholder] = key
        if isinstance(value, float):
            attr_values[value_placeholder] = Decimal(str(value))
        else:
            attr_values[value_placeholder] = value

    assignments_table.update_item(
        Key={"assignment_id": assignment_id},
        UpdateExpression="SET " + ", ".join(update_parts),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


def fail_assignment_if_not_terminal(assignment_id: str, error: str) -> bool:
    """Mark an assignment failed ONLY if the agent hasn't already recorded a
    terminal outcome. Returns True if this write landed.

    The invoke call and the agent's own status writes RACE: the runtime can
    finish the work, write ``completed`` (or pause with ``awaiting_input``),
    and then die during response/teardown — surfacing an invoke exception
    here AFTER the real outcome landed. An unconditional ``failed`` write
    would clobber that outcome and tell the requester their finished work
    failed. Guard on the row still being in a pre-terminal state."""
    try:
        assignments_table.update_item(
            Key={"assignment_id": assignment_id},
            UpdateExpression="SET #s = :failed, result_summary = :rs, completed_at = :now",
            ConditionExpression="#s IN (:dispatched, :resuming)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":failed": "failed",
                ":rs": str(error)[:1000],
                ":now": int(time.time()),
                ":dispatched": "dispatched",
                ":resuming": "resuming",
            },
        )
        return True
    except assignments_table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "assignment %s already reached a terminal state — invoke error "
            "not recorded over it (%s)",
            assignment_id,
            str(error)[:200],
        )
        return False


# --- Agent Invocation --------------------------------------------------------


def thread_runtime_session_id(
    agent_id: str, source: str, source_context: dict | None
) -> str | None:
    """A deterministic AgentCore ``runtimeSessionId`` for a Slack thread.

    AgentCore keeps a runtime session's microVM warm for ~15 minutes of
    inactivity; invoking with the SAME runtimeSessionId lands on that warm
    session. Deriving the id from (workspace, channel, thread, agent) means a
    thread reply — a resume answer or a follow-up — reuses the invocation
    environment that served the first request while it's still hot: no cold
    start, and the working tree from the earlier segment is still on disk.
    The S3 conversation session remains the durable fallback when the microVM
    has expired.

    Per-agent on purpose: two agents serving one thread get separate sessions.
    Returns None off-thread (the caller falls back to a per-assignment id).
    """
    if source != "slack":
        return None
    ctx = source_context or {}
    thread_key = enrichment.slack_thread_key(
        str(ctx.get("workspace", "") or ""),
        str(ctx.get("channel_id", "") or ""),
        str(ctx.get("thread_ts", "") or ""),
    )
    if not thread_key:
        return None
    digest = hashlib.sha256(
        f"{thread_key}#{agent_id}".encode()
    ).hexdigest()
    return f"thread-{digest}"  # 71 chars — within the API's 33–256 bound


def _invoke_runtime(runtime_arn: str, payload: bytes, session_id: str, fallback_session_id: str):
    """InvokeAgentRuntime with session affinity + busy-session fallback."""
    base_kwargs = {
        "agentRuntimeArn": runtime_arn,
        "qualifier": "DEFAULT",
        "contentType": "application/json",
        "accept": "application/json",
        "payload": payload,
    }
    try:
        agentcore.invoke_agent_runtime(**base_kwargs, runtimeSessionId=session_id)
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code", "")
        if code != "RetryableConflictException" or session_id == fallback_session_id:
            raise
        logger.info(
            "runtime session %s busy — falling back to fresh session %s",
            session_id,
            fallback_session_id,
        )
        agentcore.invoke_agent_runtime(**base_kwargs, runtimeSessionId=fallback_session_id)


def invoke_agent(
    agent_config: dict,
    instruction: str,
    source: str,
    source_context: dict,
    assignment_id: str,
):
    """Invoke an AgentCore Runtime agent."""
    runtime_arn = agent_config["runtime_arn"]

    if "${" in runtime_arn or not runtime_arn.startswith("arn:"):
        raise ValueError(
            f"Agent '{agent_config.get('agent_id', '?')}' has an unresolved runtime_arn "
            f"({runtime_arn!r}). The dashboard renders only capabilities with a live "
            "runtime into the registry; re-onboard the agent if this persists."
        )

    payload = json.dumps(
        {
            "prompt": instruction,
            "session_id": assignment_id,
            "source": source,
            "source_context": source_context,
            "assignment_id": assignment_id,
        }
    ).encode("utf-8")

    # Thread-stable session id (Slack) so replies in the thread reuse the warm
    # runtime session; per-assignment id (a 36-char UUID) everywhere else.
    session_id = (
        thread_runtime_session_id(
            agent_config.get("agent_id", ""), source, source_context
        )
        or assignment_id
    )
    _invoke_runtime(runtime_arn, payload, session_id, assignment_id)


# --- Guardrail Block Handling ------------------------------------------------


def _put_metric(name: str, dimensions: dict | None = None, value: float = 1.0):
    """Emit a CloudWatch metric. Swallows errors — observability is best-effort."""
    dims = [{"Name": "Stage", "Value": STAGE}]
    if dimensions:
        dims.extend({"Name": k, "Value": v} for k, v in dimensions.items())
    try:
        cloudwatch.put_metric_data(
            Namespace=CLOUDWATCH_NAMESPACE,
            MetricData=[
                {
                    "MetricName": name,
                    "Dimensions": dims,
                    "Value": value,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as exc:
        logger.warning("Failed to emit metric %s: %s", name, exc)


def _post_block_reply(
    source: str, source_context: dict, message: str, *, agent_id: str | None = None
) -> bool:
    """Post the block-notice message to the originating thread. Returns True on success."""
    if source == "github":
        return reply.post_github_comment(
            repo=source_context.get("repo", ""),
            issue_number=source_context.get("issue_number", ""),
            body=message,
        )
    if source == "asana":
        return reply.post_asana_comment(
            task_gid=source_context.get("task_gid", ""),
            body=message,
        )
    if source == "slack":
        return reply.post_slack_message(
            team_id=source_context.get("workspace", ""),
            channel=source_context.get("channel_id", ""),
            body=message,
            thread_ts=source_context.get("thread_ts"),
            agent_id=agent_id,
        )
    if source == "jira":
        return reply.post_jira_comment(
            site_id=source_context.get("workspace", ""),
            issue_key=source_context.get("issue_key", ""),
            body=message,
        )
    if source == "confluence":
        return reply.post_confluence_comment(
            site_id=source_context.get("workspace", ""),
            page_id=source_context.get("page_id", ""),
            body=message,
            parent_comment_id=source_context.get("comment_id"),
        )
    logger.warning("No reply channel for source=%s — block notice not posted", source)
    return False


def _notify_fleet_event(
    *, tier: str, event: str, text: str, source: str, sender: str, source_context: dict
) -> None:
    """Fan a fleet lifecycle event out to subscribed Slack channels (spec §18.3).
    Best-effort — never let a notification failure affect dispatch. The actor
    (for actionable/error @mentions) is the dispatch requester, resolved to the
    right Slack user per workspace by the identity map. Repo scope is the GitHub
    repo when present (so repo-scoped subscriptions match)."""
    try:
        # Pass the RAW sender (as resolve_dispatch_identity / ensure_user_request
        # do). _identity_source_and_handle already namespaces internally; feeding
        # it a pre-namespaced principal would double-prefix github/asana handles
        # (github:github:<login>) so the identity-map @mention lookup never
        # matches and error/actionable posts silently degrade to unmentioned.
        id_source, handle, workspace = _identity_source_and_handle(
            sender, source, source_context
        )
        notify.notify(
            tier=tier,
            event=event,
            text=text,
            repo=str(source_context.get("repo", "") or ""),
            # Slack conversations thread on the conversation key so linked
            # follow-up assignments continue the same ops-channel thread.
            unit=notify.unit_for(
                source_context.get("assignment_id", "") or "", source_context
            ),
            actor={"source": id_source, "handle": handle, "workspace": workspace},
        )
    except Exception:
        logger.exception("fleet-event notification failed (%s/%s)", tier, event)


# --- Resume dispatch (durable-repo-work spec) ---------------------------------


def _resume_lock(assignment_id: str, *, reply_text: str = "", sender: str = "") -> bool:
    """Conditionally flip ``awaiting_input → resuming`` — the resume lock. A
    second fast reply loses the conditional write and is rejected (the winner
    is already feeding the agent). Returns True when this caller holds it.

    The winning reply is appended to the run's ``timeline`` in the SAME write,
    so the dashboard's turn-by-turn view records exactly the answer that fed
    the agent (a losing racer's text never lands)."""
    try:
        assignments_table.update_item(
            Key={"assignment_id": assignment_id},
            # resume_started_at bounds the sweeper's stuck-resume window (a
            # resume whose agent never started reverts to awaiting_input).
            UpdateExpression=(
                "SET #s = :resuming, resume_started_at = :now, "
                "timeline = list_append(if_not_exists(timeline, :empty), :evt)"
            ),
            ConditionExpression="#s = :awaiting",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":resuming": "resuming",
                ":awaiting": "awaiting_input",
                ":now": int(time.time()),
                ":empty": [],
                ":evt": [
                    {
                        "ts": int(time.time()),
                        "kind": "reply",
                        "actor": sender,
                        "text": (reply_text or "")[:2000],
                    }
                ],
            },
        )
        return True
    except assignments_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def handle_resume(event) -> dict:
    """Resume a paused (``awaiting_input``) assignment from an in-thread reply.

    The Slack webhook resolved the thread binding and sends
    ``{"resume_of": <assignment_id>, "body": <the reply>, "sender": ..., ...}``.
    Order mirrors the main dispatch pipeline stage for stage — a resume IS a
    dispatch: identity resolution (so email/group-granted access applies to
    the reply exactly as it did to the original dispatch, and a pending user
    can't resume what they couldn't start), trigger-authz on the replier,
    guardrail on the reply text (untrusted input — no bypass), THEN the
    conditional ``awaiting_input → resuming`` flip (the lock), THEN re-invoke
    the SAME runtime with the saved interrupt id + workspace snapshot + the
    reply."""
    assignment_id = str(event.get("resume_of", ""))
    reply_text = event.get("body", "") or ""
    sender = event.get("sender", "unknown")
    source_context = event.get("context", {}) or {}

    row = (
        assignments_table.get_item(Key={"assignment_id": assignment_id}).get("Item")
        or {}
    )
    if not row:
        return _error(404, f"assignment {assignment_id} not found")
    agent_id = str(row.get("agent_id", ""))
    if row.get("status") != "awaiting_input":
        return _error(
            409,
            f"assignment {assignment_id} is not awaiting input (status: {row.get('status')})",
        )

    registry = load_registry()
    agents = registry.get("agents", {})
    if agent_id not in agents:
        return _error(404, f"agent {agent_id} is no longer routable")
    agent_config = {**agents[agent_id], "agent_id": agent_id}

    # Identity resolution + onboarding gate — same as the main pipeline (§16).
    # Without it, a user whose access comes from an email- or group-scoped
    # grant would be denied on their own agent's question (their groups are on
    # the identity, not the raw principal), and a pending/not-onboarded user
    # could resume work they could never dispatch.
    principal = namespaced_principal(sender, "slack")
    if principal and principal not in _UNRESOLVED_SENDERS:
        person = resolve_dispatch_identity(sender, "slack", source_context)
        if not person.usable:
            _put_metric(
                "UserNotOnboarded", dimensions={"Source": "slack", "AgentId": agent_id}
            )
            _post_block_reply(
                "slack",
                source_context,
                onboarding_reply("slack", source_context, True),
                agent_id=agent_id,
            )
            return _error(
                403, f"sender '{sender}' is not onboarded (identity {person.status})"
            )
        if person.email:
            source_context["requester_email"] = person.email
        if person.groups:
            existing_groups = list(source_context.get("principal_groups") or [])
            source_context["principal_groups"] = sorted(
                {*existing_groups, *person.groups}
            )

    # Authorize the REPLIER — a resume is a dispatch, and anyone in the thread
    # can type. Same Cedar trigger-authz as any dispatch (fail-closed on an
    # unresolved sender), so a pause can't be hijacked by an unauthorized user
    # steering the agent's next steps.
    authorized, authz_reason = authorize_trigger(
        agent_config, sender, "slack", source_context
    )
    if not authorized:
        _put_metric(
            "TriggerDenied",
            dimensions={"Source": "slack", "AgentId": agent_id, "Reason": authz_reason},
        )
        _post_block_reply(
            "slack",
            source_context,
            f"⛔ You're not authorized to resume this work (reason: {authz_reason}). "
            "The original requester (or anyone with access) can reply instead.",
            agent_id=agent_id,
        )
        return _error(
            403, f"user '{sender}' not authorized to resume @{agent_id} ({authz_reason})"
        )

    # The reply is untrusted input — same guardrail as any dispatch (no bypass).
    guardrail_result = guardrail.check_prompt(reply_text)
    if guardrail_result.outcome != "passed":
        _put_metric(
            "GuardrailTripped" if guardrail_result.outcome == "blocked" else "GuardrailError",
            dimensions={"Source": "slack", "AgentId": agent_id},
        )
        message = (
            BLOCKED_MESSAGE_TEMPLATE.format(
                reason_suffix=f" ({guardrail_result.reason})" if guardrail_result.reason else "",
                assignment_id=assignment_id,
            )
            if guardrail_result.outcome == "blocked"
            else GUARDRAIL_ERROR_MESSAGE_TEMPLATE.format(assignment_id=assignment_id)
        )
        _post_block_reply("slack", source_context, message, agent_id=agent_id)
        return _error(400, f"resume reply blocked by guardrail: {guardrail_result.reason}")

    # Concurrency — a resumed run occupies a runtime invocation exactly like a
    # fresh dispatch (paused rows don't count as 'dispatched', so without this
    # gate N simultaneous replies could stack N invocations past the cap).
    max_concurrent = agent_config.get("limits", {}).get("max_concurrent", 5)
    if not check_concurrency(agent_id, max_concurrent):
        _post_block_reply(
            "slack",
            source_context,
            f"⏳ @{agent_id} is at capacity right now — your answer wasn't lost; "
            "reply again in a few minutes to resume.",
            agent_id=agent_id,
        )
        return _error(
            429, f"@{agent_id} is at capacity ({max_concurrent} active); resume deferred"
        )

    # The lock: only one reply resumes; a racing second reply is rejected.
    if not _resume_lock(assignment_id, reply_text=reply_text, sender=sender):
        return _error(409, f"assignment {assignment_id} is already resuming")

    resume_payload = {
        "interrupt_id": str(row.get("interrupt_id", "")),
        "response": reply_text,
        "workspace_snapshot": row.get("workspace_snapshot") or [],
    }
    original_context = row.get("source_context") or {}
    payload = json.dumps(
        {
            "prompt": reply_text,
            "session_id": assignment_id,
            "source": row.get("source", "slack"),
            "source_context": original_context,
            "assignment_id": assignment_id,
            "resume": resume_payload,
        }
    ).encode("utf-8")
    # Session affinity: derive the SAME thread-stable runtimeSessionId the
    # original dispatch used, so a reply within AgentCore's ~15-min idle
    # window lands on the still-warm microVM that paused — conversation and
    # working tree intact in place. Past the window (or if the session is
    # busy), a fresh session cold-starts and restores from S3/the snapshot.
    session_id = (
        thread_runtime_session_id(
            agent_id, str(row.get("source", "slack")), original_context
        )
        or assignment_id
    )
    try:
        _invoke_runtime(
            agent_config["runtime_arn"], payload, session_id, assignment_id
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to resume agent %s: %s", agent_id, e)
        # Same no-clobber rule as the dispatch path: the resumed agent may
        # have finished (or re-paused) before the invoke error surfaced.
        fail_assignment_if_not_terminal(assignment_id, f"resume failed: {e}")
        return _error(500, f"failed to resume @{agent_id}: {e}")

    _post_block_reply(
        "slack",
        source_context,
        f"▶️ Picking the work back up with your answer. (assignment `{assignment_id}`)",
        agent_id=agent_id,
    )
    _put_metric("AssignmentResumed", dimensions={"AgentId": agent_id})
    logger.info("Resumed assignment %s (agent %s)", assignment_id, agent_id)
    return {
        "statusCode": 200,
        "body": json.dumps({"assignment_id": assignment_id, "status": "resuming"}),
    }


# --- Lambda Handler ----------------------------------------------------------


def handler(event, context):
    """Dispatch Router Lambda entry point.

    Receives normalized events from:
    - GitHub Actions (via AWS CLI lambda invoke)
    - Asana webhook receiver Lambda (via direct invoke)
    - Slack Events API (via API Gateway)

    Expected event shape:
    {
        "source": "github" | "asana" | "slack",
        "trigger_type": "comment_mention" | "assignment" | "custom_field" | "slash_command",
        "body": "the comment/message text",
        "sender": "username",
        "context": {
            // source-specific context
            "repo": "owner/repo",           // github
            "issue_number": "123",          // github
            "task_gid": "12345",            // asana
            "task_name": "...",             // asana
            "task_notes": "...",            // asana
            "channel_id": "C123",           // slack
            "thread_ts": "...",             // slack
        }
    }
    """
    logger.info("Dispatch event: %s", json.dumps(event))

    # Resume dispatch (durable-repo-work spec): an in-thread reply on a paused
    # assignment re-enters here with resume_of set by the Slack webhook's
    # thread-binding lookup. Separate path — same runtime, same assignment.
    if event.get("resume_of"):
        return handle_resume(event)

    registry = load_registry()

    source = event.get("source", "unknown")
    trigger_type = event.get("trigger_type", "comment_mention")
    body = event.get("body", "")
    sender = event.get("sender", "unknown")
    source_context = event.get("context", {})

    # --- Resolve agent ---
    # If agent_id is pre-resolved (e.g. by Asana webhook receiver), use it directly.
    # Otherwise, parse @mention from body.
    pre_resolved_id = event.get("agent_id")
    if pre_resolved_id:
        agents = registry.get("agents", {})
        if pre_resolved_id not in agents:
            return _error(404, f"unknown agent: {pre_resolved_id}")
        agent_config = {**agents[pre_resolved_id], "agent_id": pre_resolved_id}
        instruction = event.get(
            "instruction",
            body
            or f"You have been assigned to task: {source_context.get('task_name', 'unknown')}",
        )
    else:
        # --- Parse @mention from body ---
        agent_config, instruction = extract_mention_and_instruction(body, registry)
        if agent_config is None:
            return _error(400, "no recognized @agent mention found")

    agent_id = agent_config["agent_id"]

    # --- Identity resolution + onboarding gate (spec §16) ---
    # Resolve (get-or-create + enrich) the person behind this dispatch from ANY
    # source. A first touch from an unknown/pending user is NOT usable: we file a
    # one-per-identity onboarding request and reply (every time) telling them to
    # get onboarded — the org/personal-repo copy differs on whether we can email
    # them. A resolved, ACTIVE identity's email + groups feed authorization so a
    # grant authored against an email/group applies across all their sources.
    principal = namespaced_principal(sender, source)
    # A synthetic automation principal has no person behind it: skip identity
    # resolution + the first-touch onboarding gate, but still authorize it
    # through AVP below (against its auto-authored grant, §A8.4). Gate the skip on
    # BOTH the reserved-namespace prefix AND trigger_type=="automation" — a real
    # sender must never be exempted from onboarding by a crafted/colliding id
    # (the automation namespace is only legitimately produced by the engine).
    is_automation = is_synthetic_principal(principal) and trigger_type == "automation"
    if principal and principal not in _UNRESOLVED_SENDERS and not is_automation:
        person = resolve_dispatch_identity(sender, source, source_context)
        if not person.usable:
            org_owned = _is_org_owned(source, source_context)
            if person.identity_id:
                identity_map.ensure_user_request(
                    identity_id=person.identity_id,
                    source=_identity_source_and_handle(sender, source, source_context)[0],
                    source_context=source_context,
                    proposed_email=person.email,
                    display_name=person.display_name,
                )
            _put_metric(
                "UserNotOnboarded",
                dimensions={"Source": source, "AgentId": agent_id},
            )
            # Reply EVERY time (the user needs the feedback loop); the request is
            # deduped to one row by ensure_user_request.
            if not _post_block_reply(
                source, source_context, onboarding_reply(source, source_context, org_owned)
            ):
                _put_metric("OnboardingReplyFailed", dimensions={"Source": source})
            return _error(
                403, f"sender '{sender}' is not onboarded (identity {person.status})"
            )
        # Active identity — thread email + group membership into the authz context
        # so trigger_authz evaluates them (email = requester_email attr; groups
        # merged with any channel/context groups the receiver already stamped).
        if person.email:
            source_context["requester_email"] = person.email
        if person.groups:
            existing_groups = list(source_context.get("principal_groups") or [])
            source_context["principal_groups"] = sorted(
                {*existing_groups, *person.groups}
            )

    # --- Authorization ---
    # Cedar-backed trigger authz (the SdlcTrigger AVP store), keyed on sender +
    # agent + source/workspace/channel. Same path for every source; no allowlist.
    authorized, authz_reason = authorize_trigger(
        agent_config, sender, source, source_context
    )
    if not authorized:
        _put_metric(
            "TriggerDenied",
            dimensions={"Source": source, "AgentId": agent_id, "Reason": authz_reason},
        )
        # Post a specific reject notice to the originating thread so the sender
        # isn't left with silence (best-effort; the denial stands regardless).
        notice = (
            f"⛔ You're not authorized to trigger @{agent_id} here "
            f"(reason: {authz_reason}). Ask an admin to grant access in the fleet dashboard."
        )
        if not _post_block_reply(source, source_context, notice):
            _put_metric("TriggerDenyReplyFailed", dimensions={"Source": source})
        return _error(
            403,
            f"user '{sender}' not authorized to invoke @{agent_id} ({authz_reason})",
        )

    # --- Repo binding (single-repo guard) ---
    if not check_repo_allowed(source, source_context):
        got = source_context.get("repo", "unknown")
        _put_metric("RepoRejected", dimensions={"Source": source, "AgentId": agent_id})
        return _error(
            403,
            f"repo '{got}' is not onboarded for this fleet, so @{agent_id} won't "
            f"act on it. Ask an admin to onboard it in the dashboard.",
        )

    # Cost attribution is handled fleet-side: agents tag model usage to the
    # fleet's single shared Mantle project (MANTLE_PROJECT_ID runtime env). A
    # dispatch may span repos (co-repo modes), so there's no per-repo project to
    # inject here.

    # --- Concurrency ---
    max_concurrent = agent_config.get("limits", {}).get("max_concurrent", 5)
    if not check_concurrency(agent_id, max_concurrent):
        return _error(
            429,
            f"@{agent_id} is at capacity ({max_concurrent} active). Try again later.",
        )

    # --- Guardrail (prompt-injection edge check, T-1/T-2/T-3) ---
    # Score the raw `body` — the text the user typed plus any surrounding
    # context that reached this Lambda. We run the check before creating an
    # assignment so a block is cheap and doesn't pollute concurrency counts
    # with aborted dispatches.
    guardrail_input = body or instruction
    guardrail_result = guardrail.check_prompt(guardrail_input)

    if guardrail_result.outcome == "blocked":
        assignment_id = create_assignment(
            agent_id=agent_id,
            source=source,
            trigger_type=trigger_type,
            requester=sender,
            instruction=instruction,
            source_context=source_context,
        )
        update_assignment(
            assignment_id,
            status="blocked_guardrail",
            result_summary=f"guardrail intervened: {guardrail_result.reason}",
            completed_at=int(time.time()),
        )
        _put_metric(
            "GuardrailTripped", dimensions={"Source": source, "AgentId": agent_id}
        )
        reason_suffix = (
            f" ({guardrail_result.reason})" if guardrail_result.reason else ""
        )
        message = BLOCKED_MESSAGE_TEMPLATE.format(
            reason_suffix=reason_suffix,
            assignment_id=assignment_id,
        )
        if not _post_block_reply(source, source_context, message):
            _put_metric("GuardrailReplyFailed", dimensions={"Source": source})
        _notify_fleet_event(
            tier=notify.TIER_ERROR,
            event="guardrail_tripped",
            text=f"⛔ A prompt-injection guardrail tripped on a request to @{agent_id}.",
            source=source,
            sender=sender,
            source_context={**source_context, "assignment_id": assignment_id},
        )
        return _error(
            400, f"@{agent_id} request blocked by guardrail: {guardrail_result.reason}"
        )

    if guardrail_result.outcome == "error":
        assignment_id = create_assignment(
            agent_id=agent_id,
            source=source,
            trigger_type=trigger_type,
            requester=sender,
            instruction=instruction,
            source_context=source_context,
        )
        update_assignment(
            assignment_id,
            status="blocked_guardrail_error",
            result_summary=f"guardrail check failed: {guardrail_result.reason}",
            completed_at=int(time.time()),
        )
        _put_metric(
            "GuardrailError",
            dimensions={"Source": source, "Reason": guardrail_result.reason},
        )
        message = GUARDRAIL_ERROR_MESSAGE_TEMPLATE.format(assignment_id=assignment_id)
        if not _post_block_reply(source, source_context, message):
            _put_metric("GuardrailReplyFailed", dimensions={"Source": source})
        return _error(503, f"guardrail check failed: {guardrail_result.reason}")

    # Slack → dashboard traceability: resolve the triggering message's shareable
    # URL once at dispatch time and persist it on the assignment. The workspace
    # domain lives only in Slack, so the dashboard can't construct this link
    # itself. Best-effort (~1 API call); "" just means no link is rendered.
    # Placed here (after all authz/guardrail gates) so we never make an API call
    # for a request we're going to reject.
    if source == "slack" and "slack_permalink" not in source_context:
        permalink = reply.slack_permalink(
            str(source_context.get("workspace", "") or ""),
            str(source_context.get("channel_id", "") or ""),
            str(
                source_context.get("message_ts", "")
                or source_context.get("thread_ts", "")
                or ""
            ),
        )
        if permalink:
            source_context = {**source_context, "slack_permalink": permalink}

    # --- Record assignment ---
    assignment_id = create_assignment(
        agent_id=agent_id,
        source=source,
        trigger_type=trigger_type,
        requester=sender,
        instruction=instruction,
        source_context=source_context,
        # D8: a reply on a completed thread is a NEW linked assignment.
        parent_assignment_id=str(event.get("parent_assignment_id", "") or ""),
    )

    # Bind the Slack thread to this assignment so an in-thread reply can
    # resume a pause (or start a linked follow-up) without naming the agent.
    if source == "slack":
        write_thread_binding(source_context, assignment_id, agent_id)

    # --- Invoke agent ---
    try:
        invoke_agent(agent_config, instruction, source, source_context, assignment_id)
    except Exception as e:
        logger.error("Failed to invoke agent %s: %s", agent_id, e)
        # The dispatched→failed status write below is a DynamoDB MODIFY that the
        # assignment-stream notifier maps to a run_failed fan-out (spec §18.1);
        # emitting run_failed here too would double-notify subscribed channels.
        # Terminal-status events belong to the stream notifier; the router only
        # emits its own pre-dispatch events (run_started, guardrail_tripped).
        #
        # CONDITIONAL: the agent may have already completed the work and
        # written its terminal status before the runtime died in teardown —
        # never clobber a real outcome with a transport error.
        if not fail_assignment_if_not_terminal(assignment_id, str(e)):
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "assignment_id": assignment_id,
                        "agent_id": agent_id,
                        "status": "completed",
                        "message": "run finished before the invoke error surfaced",
                    }
                ),
            }
        return _error(500, f"failed to invoke @{agent_id}: {e}")

    # Slack dispatches are async (the receiver already 200-acked), so unlike
    # GitHub/Asana — where the mention comment is itself the acknowledgement —
    # there's no visible confirmation unless the router posts one. Best-effort.
    # Posts under the agent's own identity (distinct username + icon).
    if source == "slack":
        _post_block_reply(
            source, source_context,
            f"🏁 On it — working on your request now. ({notify.run_ref(assignment_id)})",
            agent_id=agent_id,
        )

    _notify_fleet_event(
        tier=notify.TIER_INFORMATIVE,
        event="run_started",
        text=f"🏁 @{agent_id} started work (assignment `{assignment_id}`).",
        source=source,
        sender=sender,
        source_context={**source_context, "assignment_id": assignment_id},
    )
    logger.info("Dispatched assignment %s to %s", assignment_id, agent_id)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "assignment_id": assignment_id,
                "agent_id": agent_id,
                "status": "dispatched",
                "message": f"@{agent_id} is on it. Assignment: {assignment_id}",
            }
        ),
    }


def _error(status: int, message: str) -> dict:
    logger.warning("Dispatch error %d: %s", status, message)
    return {
        "statusCode": status,
        "body": json.dumps({"error": message}),
    }

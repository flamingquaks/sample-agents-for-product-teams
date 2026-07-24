"""Fleet Cedar policy generation for the AgentCore Gateway policy engine.

The admin allowlist (config_store) is the source of truth for which repos the
fleet may act on. This module renders that allowlist into the single, named,
fleet-owned Cedar policy SET that the Gateway policy engine enforces on GitHub
*write* tool calls — the deterministic tool-call boundary that complements the
dispatch-layer allowlist (WS3).

Enforcement model (verified against AWS docs, 2026): the engine is default-deny
+ forbid-wins, tool-call input parameters are addressed as
``context.input.<param>``, and the action name is ``<TargetName>___<toolName>``
(triple underscore) auto-generated from the gateway tool manifest. The rendered
statement is two ``forbid`` policies:
  - WRITE_TOOLS — forbidden UNLESS the call's target repo is in the allowed set
    (the per-repo allowlist boundary).
  - DESTRUCTIVE_TOOLS — forbidden UNCONDITIONALLY, for every repo. These must
    never be in WRITE_TOOLS: a per-repo forbid is LIFTED for an allowed repo, so
    a destructive tool placed there would be permitted on onboarded repos. This
    mirrors cedar/shared.cedar's unconditional destructive forbid.

Things here are inherently deploy-time-tunable against the *live* gateway tool
manifest and MUST be confirmed on first deploy (see docs/aws-deploy.md):
  1. WRITE_TOOLS / DESTRUCTIVE_TOOLS — the exact GitHub MCP tool names as exposed
     through the gateway target. The names below are the common github-mcp set;
     adjust to whatever ``tools/list`` reports.
  2. The repo parameter shape. The GitHub MCP server takes ``owner`` and
     ``repo`` as *separate* string inputs, so we condition on the (owner, repo)
     pair — Cedar can't concatenate to compare against an "owner/repo" string.
     If a given deployment's manifest exposes a combined ``repo`` param instead,
     flip ``REPO_PARAM_MODE``.
  3. Repo literals are lowercased (config_store stores lowercase); the caller
     must lowercase ``context.input.owner/repo`` for the match to hold. GitHub's
     owner/repo are case-insensitive and its MCP normalizes them.
"""

# The GitHub gateway target name — the "<TargetName>" half of the action id.
# Must match the GatewayTarget Name in infra/foundation/template.yaml.
GITHUB_TARGET = "GitHubTarget"

# GitHub write tools the repo restriction applies to. Read tools are left
# unrestricted here (the per-agent cedar/*.cedar policies still scope those).
# Keep in sync with the live gateway tool manifest — see module docstring.
# NOTE: destructive tools (see DESTRUCTIVE_TOOLS) are intentionally NOT here —
# they get an UNCONDITIONAL forbid, not a per-repo one.
WRITE_TOOLS = (
    "create_issue",
    "update_issue",
    "add_issue_comment",
    "create_pull_request",
    "create_pull_request_review",
    "create_or_update_file",
    "push_files",
    "create_branch",
    "add_labels_to_issue",
)

# GitHub READ tools — safe, unrestricted reads. Enumerated (not just "everything
# not write/destructive") so the authoring UI can positively OFFER a tool as a
# read and the API can validate a custom agent's grant against a known set (spec
# §3.5). Seeded from the reads the built-ins are granted (AGENT_TOOL_GRANTS +
# cedar/*.cedar // read blocks). Keep in sync with the live manifest —
# check_gateway_manifest.py fails on any manifest tool left unclassified.
READ_TOOLS = (
    "get_issue",
    "list_issues",
    "get_pull_request",
    "list_pull_requests",
    "get_pull_request_diff",
    "list_pull_request_files",
    "list_commits",
    "list_milestones",
    "get_file_contents",
    "search_code",
)

# Destructive GitHub tools the fleet forbids for EVERY repo, onboarded or not —
# the gateway-plane equivalent of cedar/shared.cedar's unconditional destructive
# forbid, which CLAUDE.md ("Agents NEVER close issues, merge PRs, or delete …")
# requires Cedar to enforce. These must NOT be in WRITE_TOOLS: a repo-allowlisted
# forbid is LIFTED for an allowed repo, which would permit deletion there. Names
# are the github-mcp write tools that destroy/merge/close; extend as the live
# manifest is confirmed (see module docstring). delete_file was the one this
# list originally (wrongly) allowed per-repo.
DESTRUCTIVE_TOOLS = (
    "delete_file",
    "merge_pull_request",
    "delete_branch",
)

# GitHub PR-review tools whose review *event* must be COMMENT — the fleet posts
# review comments but NEVER approves or requests changes (an approving review is
# an implicit sign-off, adjacent to the "agents NEVER … merge PRs" rule). The
# broker also hard-forces event=COMMENT (scm_broker._create_pull_request_review),
# but that is a single line of application code; this forbid is the policy-layer
# backstop so an APPROVE/REQUEST_CHANGES can't slip through if that code ever
# regresses — the same defense-in-depth the destructive tools get. Conditioned on
# the tool-call input ``event`` (addressed as context.input.event by the engine).
COMMENT_ONLY_REVIEW_TOOLS = ("create_pull_request_review",)

# --- classified tool catalog (spec §3.5) -------------------------------------
# The single classification of every GitHub tool the fleet knows: read | write |
# destructive. The three lists MUST be disjoint (a tool in two classes is an
# error — a repo-allowlisted write forbid is LIFTED for an allowed repo, so a
# destructive tool wrongly in WRITE_TOOLS would be permitted there). The authoring
# UI reads this to render each tool's read/write tag; the admin API reads it to
# reject granting a destructive tool. Note create_pull_request_review is a WRITE
# (it posts) bounded to event=COMMENT by COMMENT_ONLY_REVIEW_TOOLS, not a
# separate class. Asana tools are classified in ASANA_TOOL_CLASS below.

CLASS_READ = "read"
CLASS_WRITE = "write"
CLASS_DESTRUCTIVE = "destructive"


def _assert_disjoint() -> None:
    """Guard: the three GitHub classes must not overlap (import-time invariant)."""
    r, w, d = set(READ_TOOLS), set(WRITE_TOOLS), set(DESTRUCTIVE_TOOLS)
    for a, b, name in ((r, w, "READ/WRITE"), (r, d, "READ/DESTRUCTIVE"), (w, d, "WRITE/DESTRUCTIVE")):
        overlap = a & b
        if overlap:
            raise ValueError(f"tool catalog {name} overlap: {sorted(overlap)}")


_assert_disjoint()

# Asana tool classification. The Asana target is not repo-scoped (writes aren't
# bounded by sdlc_allowed_repos), but the read/write split still governs which
# tools a custom agent may be granted. Destructive Asana ops (delete_task, …) are
# simply never listed here, so they can't be granted.
ASANA_TOOL_CLASS = {
    "get_task": CLASS_READ,
    "list_tasks": CLASS_READ,
    "list_projects": CLASS_READ,
    "get_project_status": CLASS_READ,
    "get_task_stories": CLASS_READ,
    "search": CLASS_READ,
    "create_task": CLASS_WRITE,
    "update_task": CLASS_WRITE,
    "create_project_status": CLASS_WRITE,
    "add_comment": CLASS_WRITE,
}

_ASANA_TARGET = "AsanaTarget"


def classify_tool(action_id: str) -> str | None:
    """Classify a gateway ``Target___tool`` action id as read|write|destructive,
    or None if the tool is unknown/unclassified. Used by the admin API to reject
    granting a destructive tool and by the UI to tag tools. Unknown → None so a
    caller fails closed (an unclassified tool is not offered / not grantable)."""
    if "___" not in action_id:
        return None
    target, tool = action_id.split("___", 1)
    if target == GITHUB_TARGET:
        if tool in DESTRUCTIVE_TOOLS:
            return CLASS_DESTRUCTIVE
        if tool in WRITE_TOOLS:
            return CLASS_WRITE
        if tool in READ_TOOLS:
            return CLASS_READ
        return None
    if target == _ASANA_TARGET:
        return ASANA_TOOL_CLASS.get(tool)
    return None


def tool_catalog() -> list[dict]:
    """The full grantable tool catalog for the authoring UI: every known
    read/write tool as ``{action_id, target, tool, klass}``. Destructive tools
    are DELIBERATELY excluded — they are never grantable (§3.5)."""
    out: list[dict] = []
    for tool in READ_TOOLS:
        out.append({"action_id": f"{GITHUB_TARGET}___{tool}", "target": GITHUB_TARGET, "tool": tool, "klass": CLASS_READ})
    for tool in WRITE_TOOLS:
        out.append({"action_id": f"{GITHUB_TARGET}___{tool}", "target": GITHUB_TARGET, "tool": tool, "klass": CLASS_WRITE})
    for tool, klass in ASANA_TOOL_CLASS.items():
        out.append({"action_id": f"{_ASANA_TARGET}___{tool}", "target": _ASANA_TARGET, "tool": tool, "klass": klass})
    return out


# "pair"  → GitHub MCP exposes separate owner + repo params (the default/common
#           shape); condition on both.
# "single"→ a combined "owner/repo" repo param; condition on context.input.repo.
REPO_PARAM_MODE = "pair"

# Cedar Statement must be 35–10000 chars (AWS::BedrockAgentCore::Policy). The
# rendered statement is a Cedar policy SET (two `forbid` policies separated by
# `;`): the repo-allowlist forbid + the unconditional destructive forbid.


def _actions_block(tools) -> str:
    lines = ",\n".join(
        f'    AgentCore::Action::"{GITHUB_TARGET}___{tool}"' for tool in tools
    )
    return f"[\n{lines}\n  ]"


def _split_owner_repo(full: str) -> tuple[str, str] | None:
    """'owner/repo' → ('owner', 'repo'); None if malformed."""
    parts = full.strip().split("/", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


def _allow_condition(allowed_repos: list[str]) -> str:
    """The ``unless { ... }`` clause: the call is forbidden UNLESS its repo is in
    the allowed set. With an empty allowlist this renders ``unless { false }`` —
    i.e. all repo-scoped writes are forbidden (the safe default before any repo
    is onboarded / eligible).

    Owner/repo literals are lowercased. GitHub owner/repo is case-insensitive and
    config_store already stores repos lowercased, so this matches the casing the
    tool call carries as long as the caller lowercases ``context.input.owner/repo``
    (GitHub's MCP normalizes these). Keeping the literals lowercase keeps this
    boundary in agreement with the dispatch allowlist, which casefolds."""
    if REPO_PARAM_MODE == "single":
        if not allowed_repos:
            return "false"
        listed = ", ".join(f'"{r.casefold()}"' for r in allowed_repos)
        return f"context.input has repo &&\n  context.input.repo in [{listed}]"

    # pair mode — disjunction over (owner, repo) pairs.
    pairs = [p for p in (_split_owner_repo(r) for r in allowed_repos) if p]
    if not pairs:
        return "false"
    clauses = " ||\n    ".join(
        f'(context.input.owner == "{owner.casefold()}" '
        f'&& context.input.repo == "{name.casefold()}")'
        for owner, name in pairs
    )
    return f"context.input has owner && context.input has repo &&\n  ({clauses})"


def render_fleet_policies(allowed_repos: list[str], gateway_arn: str) -> dict[str, str]:
    """Render the fleet forbid policies, keyed by the policy name to sync each
    under. SEPARATE policies (AgentCore allows one Cedar statement per policy —
    a two-statement string is rejected with "unexpected token forbid"):

      - sdlc_allowed_repos — GitHub write tools (WRITE_TOOLS) forbidden unless the
        call's target repo is in ``allowed_repos``.
      - sdlc_forbid_noncomment_review — COMMENT_ONLY_REVIEW_TOOLS forbidden for
        EVERY repo when the review ``event`` is anything but COMMENT. The
        policy-layer backstop for the broker's forced event=COMMENT, so an
        APPROVE/REQUEST_CHANGES can't slip through even if the broker regresses.

    There is deliberately NO destructive-tools forbid policy: DESTRUCTIVE_TOOLS
    are not declared in the GitHubTarget tool schema at all (template.yaml's
    curated InlinePayload; test_scm_broker pins their absence), so they are
    structurally uncallable through the gateway — and AgentCore validates every
    Cedar action against the live tool list, REJECTING (CREATE_FAILED) any
    policy that names a tool the gateway doesn't expose. The enforcement moved
    into the target schema; DESTRUCTIVE_TOOLS remains the grant-time denylist
    (classify_tool) so a custom agent can never be granted one if a tool is
    ever added to the target.

    Each statement pins a CONCRETE resource — ``resource == AgentCore::Gateway::``
    — because AgentCore rejects a wildcard/unconstrained resource ("a wildcard
    resource was detected"). ``gateway_arn`` is the literal fleet gateway ARN.

    ``allowed_repos`` is the enabled + multi_repo_eligible + active set from
    config_store.allowed_repos(). Deterministic: same input → same output.
    """
    resource = f'AgentCore::Gateway::"{gateway_arn}"'
    allowlist_forbid = (
        "forbid(\n"
        "  principal,\n"
        f"  action in {_actions_block(WRITE_TOOLS)},\n"
        f"  resource == {resource}\n"
        f") unless {{\n  {_allow_condition(allowed_repos)}\n}};"
    )
    # Forbid a review unless its event is COMMENT. ``when { has event && event
    # != "COMMENT" }`` fires only when the caller supplies a non-COMMENT event;
    # a review with no event, or event == "COMMENT", is not caught here (it is
    # still repo-allowlist-gated by sdlc_allowed_repos).
    noncomment_review_forbid = (
        "forbid(\n"
        "  principal,\n"
        f"  action in {_actions_block(COMMENT_ONLY_REVIEW_TOOLS)},\n"
        f"  resource == {resource}\n"
        ') when {\n  context.input has event && context.input.event != "COMMENT"\n};'
    )
    return {
        "sdlc_allowed_repos": allowlist_forbid,
        "sdlc_forbid_noncomment_review": noncomment_review_forbid,
    }


# Fleet policy names that were rendered by PREVIOUS versions and must be removed
# from the engine when no longer in render_fleet_policies() — a stale (or
# CREATE_FAILED) leftover otherwise lingers forever. Only names the fleet itself
# once owned may appear here (never a foreign policy).
RETIRED_FLEET_POLICY_NAMES = ("sdlc_forbid_destructive",)


# --- per-agent permit policies -----------------------------------------------
#
# The engine is default-deny: with only the forbid policies above, an ENFORCE
# engine denies EVERY tool call. Each agent therefore needs a `permit` naming the
# tools its runtime role may call. Principals are AgentCore::IamEntity keyed on
# the runtime's assumed-role ARN (verified: IAM gateways expose the caller as
# arn:aws:sts::<acct>:assumed-role/<role>, session-name-independent), the action
# is <Target>___<tool>, and the resource is the literal gateway ARN (Cedar in
# AgentCore forbids wildcard resources).
#
# AGENT_TOOL_GRANTS is the per-agent tool allowlist, translated from the intent
# in cedar/*.cedar into gateway action shape. TOOL NAMES ARE MANIFEST-DEPENDENT
# and MUST be reconciled against the live gateway `tools/list` before ENFORCE —
# see scripts/check_gateway_manifest.py and docs/aws-deploy.md. Reads + safe
# writes only; destructive tools are never granted (the forbid would win anyway).
# Researcher is Asana-only (its web_search / code-exec are local tools, not MCP
# through the gateway), so it gets only Asana actions.
_GH = GITHUB_TARGET
_AS = "AsanaTarget"  # must match the AsanaGatewayTarget Name in template.yaml

AGENT_TOOL_GRANTS = {
    "workitems": [
        f"{_GH}___get_issue",
        f"{_GH}___list_issues",
        f"{_GH}___get_pull_request",
        f"{_GH}___list_pull_requests",
        f"{_GH}___list_milestones",
        f"{_GH}___create_issue",
        f"{_GH}___update_issue",
        f"{_GH}___add_issue_comment",
        f"{_GH}___add_labels_to_issue",
        f"{_AS}___list_tasks",
        f"{_AS}___get_task",
        f"{_AS}___list_projects",
        f"{_AS}___search",
        f"{_AS}___create_task",
        f"{_AS}___update_task",
        f"{_AS}___add_comment",
    ],
    "docwriter": [
        f"{_GH}___get_file_contents",
        f"{_GH}___search_code",
        f"{_GH}___get_pull_request",
        f"{_GH}___list_pull_requests",
        f"{_GH}___get_issue",
        f"{_GH}___list_issues",
        f"{_GH}___list_commits",
        f"{_GH}___create_pull_request",
        f"{_GH}___create_issue",
        f"{_GH}___add_issue_comment",
        f"{_GH}___add_labels_to_issue",
        f"{_GH}___create_or_update_file",
        f"{_GH}___push_files",
        f"{_GH}___create_branch",
        f"{_AS}___get_task",
        f"{_AS}___list_tasks",
        f"{_AS}___search",
        f"{_AS}___add_comment",
        f"{_AS}___create_task",
    ],
    "adr": [
        f"{_GH}___get_file_contents",
        f"{_GH}___search_code",
        f"{_GH}___get_issue",
        f"{_GH}___list_issues",
        f"{_GH}___get_pull_request",
        f"{_GH}___list_pull_requests",
        f"{_GH}___get_pull_request_diff",
        f"{_GH}___list_pull_request_files",
        f"{_GH}___list_commits",
        f"{_GH}___add_issue_comment",
        f"{_GH}___add_labels_to_issue",
        # Diff-anchored PR review (Modes 2/3). The broker forces event=COMMENT,
        # so this is comment-only — never approve/request-changes/merge.
        f"{_GH}___create_pull_request_review",
    ],
    "researcher": [
        # GitHub — READ ONLY: ground research/backlog analysis in the real
        # codebase and issue tracker. No write tools (tier has no write either).
        f"{_GH}___get_file_contents",
        f"{_GH}___search_code",
        f"{_GH}___get_issue",
        f"{_GH}___list_issues",
        f"{_GH}___get_pull_request",
        f"{_GH}___list_pull_requests",
        f"{_GH}___list_commits",
        f"{_GH}___list_milestones",
        f"{_AS}___get_task",
        f"{_AS}___list_tasks",
        f"{_AS}___list_projects",
        f"{_AS}___search",
        f"{_AS}___create_task",
        f"{_AS}___update_task",
        f"{_AS}___add_comment",
    ],
}


# Per-agent GitHub App PERMISSION tier — the credential-layer form of the
# per-agent access differentiation (a docwriter may push code + open PRs; a
# workitems may only manage issues; an adr may read code + comment but never
# write code or PRs; researcher touches no GitHub). This is the SOURCE OF TRUTH,
# mirrored by infra/dispatch/scm_broker.AGENT_GITHUB_PERMISSIONS (the broker mints
# the scoped token from it); a cross-package test asserts they're equal.
# It complements AGENT_TOOL_GRANTS (the gateway tool allowlist): the Cedar grants
# scope WHICH TOOLS an agent may call at the gateway, and these permissions scope
# WHAT THE CREDENTIAL CAN DO — so even in direct mode (no gateway) an agent can't
# exceed its tier. metadata:read is implicit for every installation token.
AGENT_GITHUB_PERMISSIONS = {
    "workitems": {"issues": "write", "pull_requests": "read", "metadata": "read"},
    "docwriter": {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "metadata": "read",
    },
    "adr": {
        "contents": "read",
        "issues": "write",
        # write is required to POST a PR review. NOTE: a pull_requests:write App
        # token can itself approve/request-changes — the credential does NOT
        # bound the review event. "adr may only COMMENT" is enforced elsewhere:
        # the broker forces event=COMMENT (create_pull_request_review) and a
        # Cedar forbid (COMMENT_ONLY_REVIEW_TOOLS) rejects any non-COMMENT event.
        # Merge stays impossible because it needs contents:write, which adr lacks.
        "pull_requests": "write",
        "metadata": "read",
    },
    # READ-ONLY tier: the BA grounds backlog analysis, gap detection, and
    # prioritization in the real codebase/issue tracker. No write of any kind —
    # the broker intersects every mint with this tier, so even a write tool
    # cannot produce a write credential for researcher.
    "researcher": {
        "contents": "read",
        "issues": "read",
        "pull_requests": "read",
        "metadata": "read",
    },
}


def runtime_role_arn(account_id: str, agent: str) -> str:
    """The assumed-role ARN an agent's runtime presents to the gateway. Matches
    the ``<agent>-agentcore-runtime`` role bootstrap creates; the ``assumed-role``
    form is what AgentCore surfaces as the Cedar principal id."""
    return f"arn:aws:sts::{account_id}:assumed-role/{agent}-agentcore-runtime"


def render_agent_permit(
    agent: str, account_id: str, gateway_arn: str, actions: list[str] | None = None
) -> str:
    """Render the permit policy statement granting ``agent`` its allowed tools.

    ``actions`` overrides the tool list (a custom agent's authored ``tool_grants``,
    §3.5); when None the built-in ``AGENT_TOOL_GRANTS[agent]`` is used. A custom
    agent with an EMPTY grant renders no permit — the caller (agent_permit_policies)
    skips it, so default-deny leaves it able to call nothing. Deterministic;
    raises KeyError only for an unknown built-in agent with no override."""
    actions = AGENT_TOOL_GRANTS[agent] if actions is None else actions
    action_lines = ",\n".join(f'    AgentCore::Action::"{a}"' for a in actions)
    principal = runtime_role_arn(account_id, agent)
    return (
        "permit(\n"
        f'  principal == AgentCore::IamEntity::"{principal}",\n'
        f"  action in [\n{action_lines}\n  ],\n"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ");"
    )


def agent_permit_policies(
    account_id: str, gateway_arn: str, grants_by_agent: dict[str, list[str]] | None = None
) -> dict[str, str]:
    """All per-agent permit policies, keyed by the policy name to sync them under
    (``sdlc_permit_<agent>``).

    ``grants_by_agent`` (agent_id → tool action ids) makes the grant set
    DATA-DRIVEN so custom agents' authored ``tool_grants`` flow through the SAME
    enforcement path as the built-ins (spec §3.5) — policy_sync builds it from the
    capability rows. When None, only the hardcoded built-in ``AGENT_TOOL_GRANTS``
    are rendered (the pre-P2 behavior). An agent with an empty grant list is
    omitted (nothing to permit → default-deny)."""
    if grants_by_agent is None:
        grants_by_agent = AGENT_TOOL_GRANTS
    return {
        f"sdlc_permit_{agent}": render_agent_permit(agent, account_id, gateway_arn, actions)
        for agent, actions in grants_by_agent.items()
        if actions
    }

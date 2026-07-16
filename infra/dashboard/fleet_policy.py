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


def render_fleet_policy(allowed_repos: list[str]) -> str:
    """Render the fleet Cedar policy SET for the given allowed repo set.

    Two forbid policies:
      1. Repo-allowlist forbid — GitHub write tools (WRITE_TOOLS) are forbidden
         unless the call's target repo is in ``allowed_repos``.
      2. Unconditional destructive forbid — DESTRUCTIVE_TOOLS (delete/merge/…)
         are forbidden for EVERY repo, no exception. Kept separate so an
         allowlisted repo never lifts the destructive forbid (finding: delete_file
         was previously in the per-repo list, so an allowed repo could delete
         files, contradicting CLAUDE.md's "agents NEVER … delete").

    ``allowed_repos`` is the enabled + multi_repo_eligible + active set from
    config_store.allowed_repos(). Deterministic: same input → same output, so an
    idempotent update never churns the policy needlessly.
    """
    allowlist_forbid = (
        "forbid(\n"
        "  principal,\n"
        f"  action in {_actions_block(WRITE_TOOLS)},\n"
        "  resource\n"
        f") unless {{\n  {_allow_condition(allowed_repos)}\n}};"
    )
    destructive_forbid = (
        "forbid(\n"
        "  principal,\n"
        f"  action in {_actions_block(DESTRUCTIVE_TOOLS)},\n"
        "  resource\n"
        ");"
    )
    return f"{allowlist_forbid}\n\n{destructive_forbid}"

"""Reviewer state tools: read prior review state before reviewing, and record
the ledger after posting.

Both are structured task tools (fleet convention) — they return instructions the
LLM orchestrates through the Gateway GitHub tools + the AgentCore Memory tools,
rather than embedding GitHub/Memory business logic here. The memory namespace is
`/agents/reviewer/<repo>/pr/<number>`; the ledger entry is
{last_reviewed_sha, fingerprints:[...], resolved:[...]}.
"""

from strands import tool


@tool
def get_review_state(repo: str, pr_number: int) -> str:
    """FIRST call on every PR. Assemble the prior-review picture so you never
    re-raise a finding that already exists or a human has resolved.

    Args:
        repo: "owner/name" of the PR's repo.
        pr_number: The pull request number.

    Returns:
        Instructions for gathering existing review state.
    """
    ns = f"/agents/reviewer/{repo}/pr/{pr_number}"
    return (
        f"Assemble the prior-review state for {repo} PR #{pr_number} BEFORE "
        "reviewing anything:\n\n"
        f"1. Read the review ledger from AgentCore Memory namespace `{ns}`: LIST "
        "the recent raw events in that namespace and take the NEWEST ledger "
        "event. (The fleet Memory deliberately has no extraction strategies — "
        "the ledger needs verbatim SHAs/fingerprints — so semantic retrieval "
        "over extracted records returns nothing; the ledger lives in the raw "
        "event stream.) It holds: `last_reviewed_sha`, the list of prior finding "
        "`fingerprints`, and any `resolved` fingerprints (human dismissed/"
        "resolved). If there is no ledger event, this is a first review.\n"
        "2. Read the current PR head SHA and metadata with `get_pull_request`.\n"
        "3. Read ALL existing PR comments and review threads with the GitHub read "
        "tools. Collapse each into a finding-shaped summary and note whether the "
        "thread is resolved/unresolved.\n\n"
        "Then decide the mode:\n"
        "- If dispatched from an `@reviewer` mention → REVIEW_FULL (a mention "
        "overrides incrementality, even on an already-reviewed PR).\n"
        "- If auto-triggered on synchronize AND a ledger exists AND the head SHA "
        "equals `last_reviewed_sha` → NO-OP: post nothing, record nothing.\n"
        "- If auto-triggered on synchronize AND a ledger exists AND the SHA "
        "differs → REVIEW_INCREMENTAL over commits since `last_reviewed_sha`.\n"
        "- Otherwise → REVIEW_FULL.\n\n"
        "Carry forward the prior `fingerprints` and `resolved` set: you MUST NOT "
        "re-raise any finding whose fingerprint is already present or resolved."
    )


@tool
def record_review(repo: str, pr_number: int, head_sha: str, finding_count: int = 0) -> str:
    """LAST call, after posting the review. Write the ledger so the next run is
    incremental and won't re-raise these findings.

    Args:
        repo: "owner/name" of the PR's repo.
        pr_number: The pull request number.
        head_sha: The PR head SHA you just reviewed.
        finding_count: How many findings you posted (for the ledger + metrics).

    Returns:
        Instructions for persisting the ledger entry.
    """
    ns = f"/agents/reviewer/{repo}/pr/{pr_number}"
    return (
        f"Persist the review ledger for {repo} PR #{pr_number} to AgentCore Memory "
        f"namespace `{ns}` (record it as a raw event — the same event stream "
        "get_review_state lists):\n\n"
        f"- `last_reviewed_sha`: {head_sha}\n"
        f"- `fingerprints`: the fingerprint of EVERY finding you posted this run "
        "(file + normalized hunk + rule slug — the fingerprint helper computes "
        "these; anchor to hunk CONTENT, not line numbers, so they survive "
        "rebases), MERGED with the prior fingerprints from the ledger.\n"
        "- `resolved`: carry forward the prior resolved set unchanged.\n"
        f"- `finding_count`: {finding_count}\n\n"
        "This entry is what makes the next push incremental and stops you from "
        "re-raising the same findings. If the store fails, say so in a comment — "
        "do not silently drop it."
    )

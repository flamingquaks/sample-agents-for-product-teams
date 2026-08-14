# Reviewer Agent — Spec

**Status:** Shipped — agent + tier + Cedar + auto-trigger + commit status implemented (rollout MLP→v1.3). Pending: live single-repo eval pass on the 20-PR set.
**Trigger:** `@reviewer` mention on a GitHub pull request; `pull_request` `opened`/`synchronize` webhook events via the automation engine (per-repo, config-gated via the GitHub connector's Auto-review tab, default off)
**Source control scope:** GitHub only (for MLP; GitLab follows the fleet's generic path)

## Problem

The fleet already accelerates the *writing* side of the SDLC — Workitems hands
approved plans to `@claude`, Docwriter opens doc PRs. What it doesn't touch is
the review side, which is where agent-assisted teams actually bottleneck: agents
open PRs faster than humans can read them. Products like Cursor's Bugbot/Origin
platform exist precisely because GitHub review assumes one human reviewer
working one diff at a sequential pace.

Adr reviews PRs, but only through one lens (architecture-decision conformance).
There is no agent that reads a diff and asks the ordinary reviewer questions:
does this change break something, is it safe, is it sound?

## What Reviewer does

**On a PR (mentioned or auto-triggered):** reads the PR diff, changed files,
and the surrounding file context; reads the PR description and **all existing
PR comments and review threads first** (so it never re-raises a finding a human
or a prior run already made — this is the primary noise-reduction mechanism);
loads the repo's review rules file if present; then posts one PR review
(event=COMMENT) containing inline findings anchored to diff lines.

Each finding carries:

- **Severity** — `high` (likely bug / data loss / security) · `medium`
  (correctness risk, error-handling gap) · `low` (quality, clarity)
- **A concrete failure scenario** — inputs/state → wrong outcome. Findings the
  agent can't articulate a failure scenario for are dropped, not softened.
- **A suggested fix** as a GitHub suggestion block where the fix fits in the
  diff hunk, prose otherwise.

If there are no findings, it posts a single short summary comment saying what it
checked and that nothing rose above threshold — silence is indistinguishable
from "didn't run," so it always leaves exactly one artifact.

**On a push to an already-reviewed PR (auto mode):** reviews **incrementally**
— only the commits since the last-reviewed head SHA (from the memory ledger),
plus any prior findings whose anchor lines changed. Re-pushing an identical
diff (same head SHA) is a no-op.

## What Reviewer does NOT do

- Approve, request changes on, or merge PRs. The broker forces `event=COMMENT`
  and Cedar forbids the rest — same mechanism Adr uses today.
- Push fixes. Autofix is a v2 handoff to the existing `@claude` flow, not a
  Reviewer capability.
- Re-raise a finding a human has resolved or rejected in a review thread.
- Duplicate Adr's or a future Securityreviewer's scope — ADR conformance and
  threat modeling stay with those agents. Overlapping *findings* are fine;
  overlapping *lenses* are not.
- Review draft PRs unless the repo config opts in.

## Operating modes

One entrypoint, dispatched off the payload:

| Mode | Triggered by | Output |
|---|---|---|
| `REVIEW_FULL` | `@reviewer` mention, or automation rule on `pull_request.opened` | One PR review with inline findings across the full diff |
| `REVIEW_INCREMENTAL` | Automation rule on `pull_request.synchronize` when a prior review exists | One PR review scoped to commits since the last-reviewed SHA |

A mention on an already-reviewed PR runs `REVIEW_FULL` again — an explicit
mention is a human asking for a fresh look and overrides incrementality.

## Auto-trigger path (the platform change)

Today `github_webhook.py` handles `pull_request` events for Slack notifications
only and explicitly never dispatches an agent. This spec extends the
**automation engine** (`infra/dispatch/automation.py`, atlassian spec §A8) to
GitHub — the fast-follow its docstring already names:

1. `automation.github_facts(payload, repo_row)` — normalize a `pull_request`
   event to facts: `repo`, `pr_number`, `action`, `author`, `title`,
   `base_branch`, `head_sha`, `draft`, `labels`.
2. `github_webhook.handler` calls `automation.match_and_dispatch(connector=
   "github", ...)` after `_notify_scm`, only when the PR author is not a fleet
   bot (the existing bot-actor guard pattern).
3. An admin enables auto-review per repo by creating an `automation_rule#` row
   in the Connectors UI (`connector: github`, `event: pull_request.opened` /
   `pull_request.synchronize`, `agent: reviewer`) — a DynamoDB write, no new
   policy, synthetic `automation:github:<rule_id>` principal, existing
   cooldown / hourly-ceiling / chain-depth brakes apply unchanged.
4. Add `"github"` to `automation.TEMPLATE_VARS` (mirror in `config_store`).

Unit for cooldown is `pr:<repo>:<number>`; the head-SHA ledger (Memory, below)
makes force-push storms cheap even inside the cooldown window.

## Repo config convention

```yaml
# .pdlc-agents/review.yaml (in the target repo)
rules_file: .pdlc-agents/review.md   # freeform guidance injected into the prompt
min_severity: low                    # findings below this are dropped
review_drafts: false
max_findings: 10                     # hard cap per review; overflow noted in summary
ignore_paths:                        # globs never reviewed
  - "*.lock"
  - "dist/**"
```

`review.md` is the team's tuning surface (the `.cursor/BUGBOT.md` analog):
conventions, known false-positive patterns, "always check X" rules. Absent
config → sensible defaults, review runs anyway.

## Tools (custom Strands @tool functions)

- `get_review_state(repo, pr_number)` — returns existing review threads +
  comments (collapsed to finding-shaped summaries with resolved/unresolved
  state) + the last-reviewed SHA from memory. Called first, always.
- `plan_review(diff, files, config)` — chunks the diff into reviewable units,
  applies `ignore_paths`, returns the ordered work list. Structured task
  prompt per fleet convention — the LLM does the reviewing.
- `record_review(repo, pr_number, head_sha, findings)` — writes the ledger
  entry after posting.

GitHub reads/writes go through the existing Gateway tools: `get_pull_request`,
`get_pull_request_diff`, `list_pull_request_files`, `get_file_contents`,
`create_pull_request_review`, `add_issue_comment`. No new broker tools needed.

## Memory

- Per-PR review ledger (`/agents/reviewer/<repo>/pr/<number>`): last-reviewed
  head SHA, finding fingerprints (file + normalized anchor + rule), and
  human-resolution state. Drives incrementality and don't-re-raise.
- Fingerprints survive rebases by anchoring to hunk content, not line numbers.

## Guardrails (Cedar)

GitHub App tier (`scm_broker.AGENT_GITHUB_PERMISSIONS["reviewer"]`):
`contents: read`, `pull_requests: write`, `issues: write`, `metadata: read` —
identical to Adr's tier, and the same two enforcement points bound the review
event: broker forces `event=COMMENT`, `fleet_policy.COMMENT_ONLY_REVIEW_TOOLS`
Cedar forbid rejects anything else. No `contents: write` → merge/push stay
impossible at the credential layer.

Explicitly forbidden beyond the shared list:
- `create_or_update_file`, `push_files`, `create_branch`, `create_pull_request`
- Any label writes (Reviewer communicates only through reviews/comments)

## Infrastructure

- AgentCore Runtime: `reviewer` — pattern-identical to `adr`
- ECR: `sdlc-agents/reviewer`; onboarded via the dashboard capability flow
- Cedar policy: `cedar/reviewer.cedar` (advisory) + `fleet_policy.py` (enforced)
- Dispatch: no new receiver — `@reviewer` works day one via `mentions.py`;
  auto-trigger is the automation-engine extension above
- Threat model: new entries for auto-trigger loop risk and finding-injection
  via PR body/comments (guardrail already screens dispatch context), numbered
  after T-66

## Rollout

1. **MLP** ✅ **shipped:** `REVIEW_FULL` on `@reviewer` mention only. Agent
   (`agents/reviewer/`) + tier + Cedar (`cedar/reviewer.cedar` +
   `fleet_policy.py`) + `.pdlc-agents/review.yaml`. `@reviewer` works day one
   via `mentions.py` (no platform change). 20-PR eval set at
   `agents/reviewer/tests/eval_dataset.json`; run it against a single repo
   before broad enablement.
2. **v1.1** ✅ **shipped:** Automation-engine GitHub extension
   (`automation.github_facts` + `TEMPLATE_VARS["github"]` + `github_webhook`
   bot-guarded `_run_automation`) + `pull_request.opened` auto-review, per-repo
   opt-in via the GitHub connector's **Auto-review** tab.
3. **v1.2** ✅ **shipped (design):** `REVIEW_INCREMENTAL` on `synchronize` +
   the memory ledger (`get_review_state`/`record_review`, fingerprint helper) +
   don't-re-raise against human-resolved threads. The incrementality + ledger
   discipline live in the prompt + tools; verified by the eval set.
4. **v1.3** ✅ **shipped:** Advisory commit status on the PR head
   (`success`/`pending` only — never `failure`/`error`, so it informs but never
   gates a branch). `statuses: write` added to the reviewer broker tier + App
   permission ceiling; `create_commit_status` broker tool + Cedar clamp.
5. **v2 (deferred):** Autofix handoff — Reviewer packages confirmed findings
   into an issue and hands to `@claude` on a branch (reuses the Workitems
   handoff pattern). Dashboard "Review queue" view over open PRs with
   unresolved findings.

## Implementation map (as shipped)

| Concern | Where |
|---|---|
| Agent entrypoint + modes | `agents/reviewer/agent.py`, `prompts.py` |
| Tools | `agents/reviewer/tools/`: `review_state.py` (get/record), `plan_review.py` (diff chunk + ignore globs), `format_review.py`, `fingerprint.py` |
| Per-repo config | `agents/reviewer/project_config.py` (`.pdlc-agents/review.yaml`) |
| GitHub App tier | `scm_broker.AGENT_GITHUB_PERMISSIONS["reviewer"]` = `fleet_policy` mirror (cross-package equality test) |
| Gateway tool grants | `fleet_policy.AGENT_TOOL_GRANTS["reviewer"]` |
| Commit-status tool | `scm_broker._create_commit_status` + `create_commit_status` in `WRITE_TOOLS`, template inline schema |
| Cedar (advisory) | `cedar/reviewer.cedar` |
| Auto-trigger | `automation.github_facts` / `match_and_dispatch(connector="github")`, `github_webhook._run_automation` (bot-actor guard) |
| Auto-review UI | `dashboard/src/connectors/GitHubAutomationsTab.tsx` |
| Built-in seed | `scripts/deploy_fleet.py::_BUILTIN_AGENTS["reviewer"]` |

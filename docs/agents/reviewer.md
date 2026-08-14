# Reviewer

**Role:** Code-review agent — reads a PR diff and posts inline findings for correctness, safety, and soundness (the fleet's answer to Cursor Bugbot/Origin)
**Status:** Shipped — agent + tier + Cedar + auto-trigger + commit status implemented; pending a live single-repo eval pass
**Trigger:** `@reviewer` mention on a GitHub pull request; optional per-repo auto-review on `pull_request.opened`/`synchronize`
**Code:** [`agents/reviewer/`](../../agents/reviewer/)
**Spec:** [`docs/specs/reviewer-agent-spec.md`](../specs/reviewer-agent-spec.md)

## What Reviewer does

Reviewer is a second pair of eyes on every diff — the review side of the SDLC, where agent-assisted teams bottleneck because code is written faster than it can be read. It reviews for the ordinary reviewer questions: does this change break something, is it safe, is it sound? It does **not** review for architecture-decision conformance (that's [Adr](adr.md)) — overlapping findings are fine, overlapping lenses are not.

On a PR (mentioned or auto-triggered) it:

- Reads the PR description and **all existing comments/review threads first**, so it never re-raises a finding a human or a prior run already made (the primary noise-reduction mechanism).
- Reads the diff, changed files, and surrounding context; loads the repo's review rules file if present.
- Posts **one PR review** (`event=COMMENT`) with inline findings anchored to diff lines. Each finding carries a **severity** (high/medium/low), a **concrete failure scenario** (inputs/state → wrong outcome — findings without one are dropped, not softened), and a **suggested fix** (a GitHub `suggestion` block where it fits the hunk, prose otherwise).
- Publishes an **advisory commit status** on the PR head (`success`/`pending` only — never a `failure`/`error` that would gate a branch).

If nothing rises above threshold, it posts exactly one short summary comment saying what it checked — silence is indistinguishable from "didn't run," so it always leaves one artifact.

## Operating modes

| Mode | Trigger | What happens |
|---|---|---|
| `REVIEW_FULL` | `@reviewer` mention, or automation rule on `pull_request.opened` | One PR review with inline findings across the full diff |
| `REVIEW_INCREMENTAL` | Automation rule on `pull_request.synchronize` when a prior review exists | One PR review scoped to commits since the last-reviewed head SHA |

A mention on an already-reviewed PR runs `REVIEW_FULL` again — an explicit mention is a human asking for a fresh look and overrides incrementality. Re-pushing an identical diff (same head SHA) is a no-op.

## What Reviewer does NOT do

- Approve, request changes on, or merge PRs — the broker forces `event=COMMENT` and Cedar forbids the rest (same mechanism Adr uses). `contents:read` only, so merge/push are impossible at the credential layer.
- Push fixes. Autofix is a deferred v2 handoff to `@claude`.
- Re-raise a finding a human has resolved or rejected in a review thread.
- Review draft PRs unless the repo config opts in.

## Auto-review (per-repo, opt-in)

`@reviewer` works day one with no configuration. To review every PR automatically, an admin creates a rule in the GitHub connector's **Auto-review** tab (`connector: github`, event `pull_request.opened` / `pull_request.synchronize`, agent `reviewer`). This rides the fleet's automation engine — a synthetic `automation:github:<rule_id>` principal through the full authz/guardrail spine, with the bot-actor guard (the fleet's own PRs never trigger it), per-PR cooldown, hourly ceiling, and chain-depth cap. A `@mention` always wins over automation (explicit intent).

## Repo config convention

```yaml
# .pdlc-agents/review.yaml (in the target repo)
rules_file: .pdlc-agents/review.md   # freeform team guidance injected into the prompt
min_severity: low                    # findings below this are dropped
review_drafts: false
max_findings: 10                     # hard cap per review; overflow noted in the summary
ignore_paths:
  - "*.lock"
  - "dist/**"
```

`review.md` is the team's tuning surface (the `.cursor/BUGBOT.md` analog): conventions, known false-positive patterns, "always check X" rules. Absent config → sensible defaults, review runs anyway.

## Memory

A per-PR review ledger (`/agents/reviewer/<repo>/pr/<number>`) holds the last-reviewed head SHA, finding fingerprints (file + normalized hunk content + rule slug — anchored to content so they survive rebases), and human-resolution state. This drives incrementality and the don't-re-raise guarantee.

## Guardrails

- GitHub App tier (`contents:read`, `pull_requests:write`, `issues:write`, `statuses:write`, `metadata:read`) — identical to Adr's plus the advisory-status permission.
- Broker forces `event=COMMENT`; `fleet_policy.COMMENT_ONLY_REVIEW_TOOLS` Cedar forbid rejects anything else; the commit-status state is clamped to success/pending in both the broker and `cedar/reviewer.cedar`.
- Never granted: `create_or_update_file`, `push_files`, `create_branch`, `create_pull_request`, or any label writes.

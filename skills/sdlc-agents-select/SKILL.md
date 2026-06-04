---
name: sdlc-agents-select
description: Use when the user needs to pick which SDLC agents to install based on the tools they already use. Filters the full fleet roster against the user's integrations, presents a short opinionated recommendation, and records the selection to .sdlc-agents/selection.yaml. Invoked by sdlc-agents after tool discovery.
---

# Select the right agents for this customer

## Prerequisites

You should already know (from earlier conversation):
- PM tool in use (Asana / Jira / Linear / etc. / none)
- Source control (GitHub / GitLab / Bitbucket / etc.)
- Chat platform (Slack / Teams / etc. / none)
- Optional signals: Salesforce, Datadog/observability, Figma

If you don't know these, stop and ask the user. Do not guess.

## The roster

Match this table against what the user has. **Hide rows that require a tool the user doesn't use.** Don't present unreachable agents; it's noise.

Only agents with `Status: shipping` have working code and can be installed today. The others have design docs under `docs/agents/<name>.md` but no runtime — don't present them to the user even if their tool requirements match. If the user explicitly asks about one, tell them it's planned and point them at `docs/agents/<name>.md`.

| Agent | Status | What it does | Hard requirements | PM backends supported | Nice-to-have |
|---|---|---|---|---|---|
| `workitems` | shipping | PO/PM. Decomposes feature asks into tracker issues; status reports; risk detection. | PM tool **and** source control (GitHub can satisfy **both** at once — see note below) | **asana or github** | Slack (supported) for weekly status — any agent can also post results there |
| `researcher` | shipping | Business analyst. Research synthesis, competitive intel, backlog analysis. | PM tool | **asana or github** | Web search (Tavily, SerpAPI, Perplexity) |
| `docwriter` | shipping | Technical writer. API docs, release notes, doc PRs. | Source control | n/a — SCM-native; Asana is optional feature context | PM tool for feature context |
| `adr` | shipping | Tags issues + reviews PRs against the ADRs that govern the work. | Source control **and** an ADR directory that already exists | n/a — GitHub-native | - |

## GitHub can be both PM and source control — but only some agents have a GitHub-PM path

`workitems` needs a PM tool **and** source control. Those don't have to be two
different products. **GitHub Issues + Projects V2 is a valid PM backend**, so a
GitHub-only team satisfies both hard requirements with one tool — GitHub for SCM
*and* GitHub Issues/Projects for PM. Don't hide `workitems` from a team just
because they don't use Asana.

**Per-agent reality (do not over-promise):**
- **`workitems`** — supports `PM_BACKEND=asana` AND `PM_BACKEND=github`. Fully
  works in a GitHub-only setup.
- **`docwriter`** — SCM-native (reads code, opens doc PRs). Needs GitHub only;
  Asana is *optional* supplementary feature context. Works fine with no Asana.
- **`adr`** — GitHub-native; never needed Asana.
- **`researcher`** — supports `PM_BACKEND=asana` AND `PM_BACKEND=github`. In
  github mode it reads GitHub issues + the Projects V2 board, posts findings as
  issue comments, and creates new GitHub issues (added to the board) when
  drafting user stories. Fully works in a GitHub-only setup.

The PM backend is recorded in `selection.yaml` under `toolchain.pm`:
- `pm: asana` — Asana is the PM backend (works exactly as before; all four
  agents available).
- `pm: github` — GitHub Issues + Projects V2 is the PM backend. All four agents
  are available (each subject to its own requirements — e.g. `adr` still needs
  an ADR directory). Record the Projects V2 board number (see schema).
  `workitems` and `researcher` run with `PM_BACKEND=github`.

Asana remains fully supported; nothing about the Asana path changes.

## ADR agent has a repo-specific check

The `adr` agent is useful only when the **target repo** already has an ADR library — it can't tag work against decisions that don't exist. Before including `adr` in any recommendation, confirm the target repo actually has ADRs.

**Check the target repo, not the installer cwd.** The top-level flow resolves `TARGET_REPO` as an absolute path before invoking this skill; all filesystem checks here run against that path. If `TARGET_REPO` isn't set, stop and ask the top-level flow to resolve it — don't fall back to `ls` in the current directory, since that will misdetect when the installer and target are different repos.

Check the common ADR locations under `TARGET_REPO` (for example `"$TARGET_REPO/adrs"`, `"$TARGET_REPO/ADRs"`, `"$TARGET_REPO/docs/adrs"`, `"$TARGET_REPO/docs/decisions"`, `"$TARGET_REPO/architecture/decisions"`). If one is present and non-empty, report which path you found and ask the user to confirm it's the right one. If none are present, ask the user whether they keep ADRs at a non-standard path before concluding there are none.

- **Yes, at one of those paths:** include `adr` in Optional. Record the path in `selection.yaml` under `adr.dir` (relative to the target repo root).
- **Yes, at a non-standard path** (e.g. `architecture/decisions/`, `docs/decisions/`): include `adr` in Optional; record the custom path.
- **No ADRs yet:** omit `adr` from the presentation entirely. Don't recommend it, don't list it as skipped — the user doesn't need to see it as an option. If they explicitly ask about it, tell them: *"The `adr` agent needs an ADR library in the target repo to link against. Start writing ADRs in `docs/adrs/` there first, then re-run selection."*

The agent has no fallback behavior for "no ADRs" — it's not useful without them, so don't ship it half-configured.

## Present to the user

1. Filter the table to agents that are `shipping` **and** whose hard requirements are met. The one backend-specific exclusion:
   - For `adr`, apply the ADR-directory check above — don't show it if the repo doesn't have ADRs.
   (`workitems`, `researcher`, and `docwriter` all work under either PM backend; no PM-backend exclusion applies to them.)
2. Group into two categories — **Recommended** and **Optional** — and print each with a one-line purpose. The Recommended bucket should be anchored to what tools the user has. For a GitHub-only stack (no Asana), e.g.:

   > Based on your setup (GitHub Issues + Projects V2 + Slack), I recommend starting with:
   > - **workitems** — decomposes board items into GitHub issues, runs the work loop on the Projects V2 board, posts status to Slack
   > - **researcher** — synthesizes research into GitHub issues on the board; competitive intel and backlog analysis
   > - **docwriter** — opens GitHub doc PRs on merged code PRs
   >
   > Optional add-ons that fit your stack: `adr` (if you have an ADR library).

   Or, for an Asana + GitHub + Slack stack, all four are available too. If the user has Slack, note that any selected agent can be reached from and post results to Slack once they run `sdlc-agents-connect-slack`.

3. Ask the user which to install. Accept three answers:
   - "just the recommended" → select the Recommended list
   - an explicit list (space- or comma-separated agent names)
   - "all" → select Recommended + Optional

4. Before recording, warn on redundant combinations:
   - `adr` without an ADR directory → won't find anything to link against (the ADR-directory check above should already have removed it)

## Record the selection

Write `.sdlc-agents/selection.yaml` at the **target project root** (`$TARGET_REPO/.sdlc-agents/selection.yaml`), not in the installer repo:

```yaml
# Generated by sdlc-agents-select. Edit by re-running the skill.
selected_at: 2026-04-29T00:00:00Z
toolchain:
  # `pm` may be `asana` (default) or `github` (GitHub Issues + Projects V2 as the
  # PM backend). jira/linear/trello/aha are not yet supported end-to-end.
  pm: asana         # or: github, jira, linear, trello, aha, none
  scm: github       # or: gitlab, bitbucket
  chat: slack       # or: teams, none
  support: null     # or: salesforce
  observability: null  # or: datadog, cloudwatch, grafana
# When pm == github, the connect-github skill records the PM backend details
# under a top-level `pm:` block (the agent runs with PM_BACKEND=github):
# pm:
#   backend: github
#   github_project_number: 7          # the Projects V2 board number
#   github_project_owner: my-org      # org or user that owns the board
agents:
  - workitems
  - docwriter
  - researcher
adr:                # only present if `adr` was selected
  dir: docs/adrs    # where the repo's ADRs live
aws:
  account_id: "123456789012"     # replace with the target AWS account
  region: <REGION>               # replace with the target region, e.g. us-west-2
  stage: dev
```

If the file exists, read it first, merge, and ask before overwriting any existing keys the user didn't re-confirm.

## What not to do

- Don't try to make every customer install the whole fleet. The interesting demo is a focused one.
- Don't reorder the Recommended list arbitrarily — keep the ordering by "dependency": `workitems` first (the orchestrator), then `researcher` (feeds workitems), then `docwriter` (fed by workitems). Dependent agents after their producers.
- Don't invent agents that aren't in the roster above. If a customer asks for an agent name we don't have (common: "@reviewer", "@qa"), tell them the closest match or flag it as a gap.

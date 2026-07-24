# Durable Repo Work + Human-in-the-Loop Resume — Design Spec

Status: **APPROVED — Phases 1+2 implemented** (Phase 3 watchdog/TTL pending) · Owner: fleet · Depends on: `dispatch-agent-assignment-spec.md`, `slack-connectors-spec.md`

Implementation map: `agents/shared/tools/workspace.py` (git workspace + wip
branches), `agents/shared/durable.py` (S3 sessions, `ask_user` interrupt,
pause/resume protocol), `infra/dispatch/workspace_token_vendor.py` (scoped
clone/push credential minting), router `handle_resume` + thread bindings,
Slack webhook bound-thread routing, notifier `awaiting_input` copy.

## Problem

Two gaps, discovered together:

1. **Repo access is API-only.** The SCM broker exposes GitHub REST calls
   (`get_file_contents`, `search_code`, `push_files`, …) but there is **no clone,
   no working tree, no `git`, no build/test loop**. Agents scoped for repo work
   ("full clone, work on a project, run tests, open a PR") cannot do it — the
   runtime image is `python:3.12-slim` with no `git` and no writable project
   space, and AgentCore Runtime is request/response, not a session sandbox.

2. **No way to pause for a human and resume.** When an agent needs input it can
   only fail or guess. AgentCore **idle-stops the runtime** — a paused agent's
   container is torn down, and the user may not reply for hours or days. Both
   the conversation and any in-progress code work are lost.

These share one spine: the **assignment**. `assignment_id` already anchors the
unit of work, binds to the Slack thread, and flows through the notifier.
Everything here hangs off it.

## Decisions (locked)

| # | Decision |
|---|----------|
| D1 | **Ephemeral workspace.** Disk is scratch. Nothing survives container exit unless pushed to GitHub or written to a durable store. |
| D2 | **Conversation durability = Strands `S3SessionManager`**, `session_id=assignment_id`. Persists messages + tool state every turn; restores automatically on cold start. |
| D3 | **Workspace durability = GitHub.** In-progress work lives on a `wip/<assignment_id>` branch, one per repo in scope. Re-clone + checkout the recorded sha on resume. No tree-snapshot to S3. |
| D4 | **Clone is lazy + scoped, not auto-on-start.** The agent clones repos from the approved set on demand (a tool). Eager-clone only when exactly one repo is in scope OR the request names a single in-scope repo. Reading one file still uses the API tool — no clone. |
| D5 | **Pause = Strands interrupt** (`event.interrupt(...)` from a `BeforeToolCallEvent` hook), not a checkpoint marker. Interrupts are the documented human-in-the-loop primitive and are session-managed across the return→response gap. |
| D6 | **Resume trigger = in-thread `@sdlc-agents` mention.** The thread is bound to the assignment; the mention need not name the agent (resolved from the binding). The pause message tells the user to reply with `@sdlc-agents` in-thread. |
| D7 | **Pause requires a clean push.** Before a pause, every in-scope repo must be committed clean and pushed (local sha == remote sha). If the push cannot land after retries, **fail loud** (`failed`, recoverable message to the thread) — never exit-and-lose. |
| D8 | **A reply on a `completed` thread → new assignment**, `parent_assignment_id` linking to the prior, prior conversation loaded as read-only context, fresh clone. Not a reopen. |

## Two durability problems, two resume triggers

Keep these four apart — they use different machinery:

- **Durable A — conversation** (`S3SessionManager`): automatic, every turn. Covers
  both pause and involuntary stop (idle-stop / crash / invocation ceiling).
- **Durable B — workspace** (GitHub wip branch): manual, checkpointed only at a
  pause. Only the pause case must be lossless.
- **Trigger 1 — voluntary pause**: agent asked a question. Resumed by a Slack reply.
- **Trigger 2 — involuntary stop**: runtime replaced/crashed. Resumed by a watchdog,
  no human. (Phase 3 — the checkpoint format must support it from day one, but
  the human-reply path ships first.)

## Strands mechanics (from the API docs)

- **`S3SessionManager(session_id, bucket, prefix=..., region_name=...)`** —
  `session_id` **must not contain path separators**; `assignment_id` (UUID) is
  safe. Agent constructed with it **auto-restores prior messages** on init in a
  new process. **Direct `agent.messages` mutation is NOT persisted** — so the
  human's reply must re-enter via the interrupt-response payload, not by
  appending to messages.
- **Interrupt**: `event.interrupt(name, reason)` raised from a
  `BeforeToolCallEvent` hook → `AgentResult.stop_reason == "interrupt"`, pending
  ones on `result.interrupts`. Resume: `agent([{"interruptResponse":
  {"interruptId": <id>, "response": <text>}}])`. Interrupts are
  "session-managed in-between return and user response" — so `S3SessionManager`
  persists the pending interrupt across container death; we persist only the
  `interrupt_id` string on the row to rebuild the payload cold.
- **`EventLoopMetrics` resets per invocation** → resumed runs must **accumulate**
  token/cost onto the row, not overwrite (see Cost, below).

## The `ask_user` pause protocol

`ask_user` is a tool the agent calls when it needs human input. A
`BeforeToolCallEvent` hook intercepts it and raises the interrupt. Before the
loop returns and the container exits, in this **load-bearing order**:

1. **Push every repo clean.** For each cloned repo in scope: commit dirty tree
   to `wip/<assignment_id>`, push, verify `local sha == remote sha` and nothing
   unpushed. (D7: push must succeed or we fail loud.)
2. **Record `workspace_snapshot`** on the assignment row:
   `[{repo, branch, sha}, ...]`.
3. **Record `interrupt_id`** and the question text on the row.
4. **Flip status → `awaiting_input`.** This is the commit point — the flip is
   what makes the thread resumable. Must come AFTER 1–3 so a fast reply can't
   race a half-pushed branch.
5. **Return / exit.**

`S3SessionManager` has already persisted the conversation + pending interrupt
continuously, so there is no separate "save conversation" step.

## Resume protocol (Slack reply)

1. Webhook receives `app_mention` in a thread → looks up
   `thread_binding#<team>#<channel>#<thread_ts>`.
2. Binding present + `awaiting_input` → **resume dispatch** (same
   `assignment_id`, mention text = the answer, agent resolved from the binding,
   NOT from the mention text). Binding + `completed` → **new linked assignment**
   (D8). No binding → normal new dispatch.
3. Guardrail still runs on the reply (untrusted input — no bypass).
4. Conditional write flips `awaiting_input → resuming` (the lock; a second fast
   reply is rejected/queued).
5. New container: construct agent with `S3SessionManager(assignment_id)`
   (restores conversation + pending interrupt), re-clone each repo from
   `workspace_snapshot` and checkout the sha, resume with
   `[{"interruptResponse": {"interruptId": <saved id>, "response": <answer>}}]`.
6. On task completion: squash `wip/<assignment_id>` into the real PR; delete wip
   branches; leave the S3 session (short TTL cleanup).

## Loss boundary (stated plainly)

- **Voluntary pause**: clean commit+push first → **nothing lost**.
- **Involuntary crash mid-edit**: conversation restored from S3; workspace
  re-cloned fresh; **uncommitted edits since the agent's last commit are lost**.
  The restored conversation tells the agent what it was mid-way through, so it
  redoes that file work. Acceptable under D1 *provided* agents are prompted to
  commit at milestones. Not pretending crash-resume is lossless.

## Thread ↔ assignment binding

- On initial Slack dispatch: write
  `thread_binding#<team>#<channel>#<thread_ts> → {assignment_id, agent_id, status}`.
- Mirror of the existing `notif_thread#` row (we already store the reverse for
  notification threading).
- Lifecycle: `awaiting_input` (resumable) → `resuming` (locked) → `completed`
  (new-task-on-reply) / `timed_out` (abandoned).

## Cost accounting fix (regression this introduces)

`extract_usage()` reads `accumulated_usage` off the result. Because
`EventLoopMetrics` resets each invocation, a resumed run reports only the
resumed segment. **`complete_assignment` / the usage writer must ADD to the
row's existing `token_usage` / `input_tokens` / `output_tokens`, not overwrite.**
Every resume increments. (Direct change to the cost code added earlier this
month.)

## Runtime image + credential changes

- **`git` in the image**: base + repo-capable agent Dockerfiles must
  `apt-get install git` (currently absent). A writable workspace dir
  (e.g. `/tmp/work/<assignment_id>`; `/tmp` is writable even for the nologin user).
- **Clone/push credential**: agents hold no GitHub token by design (T-4). The
  clone/push tool mints a **short-lived, repo-scoped GitHub App token** the same
  way the broker does, uses it for the single git operation, and does not leave
  it in the container env or in `.git/config` (use an ephemeral
  `http.extraheader` or credential helper that reads the token from memory for
  the one call). This preserves the "no credential at rest in the container"
  posture.

## Open edges tracked for review

- **TTL / abandonment**: `awaiting_input` with no reply → `timed_out` sweep →
  delete wip branches + S3 session, post a "timed out, re-ask to restart" note.
- **Watchdog (Phase 3)**: scheduled sweep of `in_progress` past a heartbeat →
  re-dispatch from S3 (no human). Separate from the reply path; checkpoint
  format must already support it.
- **Concurrency**: double reply / reply-during-resume → conditional-write lock
  on the status flip.
- **Clone size / monorepo**: shallow clone (`--depth 1`); sparse-checkout if a
  co-repo scope is large. Bounded by ephemeral disk size.

## Phasing

1. **Foundation** — `git` in images + writable workspace; clone/checkout/commit/
   push tool with scoped-token minting; `S3SessionManager` wired on every agent
   keyed by `assignment_id`; cost-accumulation fix. (No pause yet — proves
   durable clone+build+PR end to end.)
2. **Pause/Resume** — `ask_user` tool + interrupt hook; pause protocol (D5/D7);
   thread binding; resume dispatch in the router; pause message copy (D6);
   `awaiting_input`/`resuming` statuses in the notifier.
3. **Watchdog + TTL** — involuntary-stop sweep; abandonment cleanup.

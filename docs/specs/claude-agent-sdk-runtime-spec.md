# Claude Agent SDK Runtime (v1.0) — SPEC / DESIGN

> **Status: DRAFT for review.** This spec exercises the `RUNTIME` seam reserved
> by `agent-authoring-spec.md §2` (and `agents/_base/agent.py:8-10`): admins can
> select, per **new or cloned** agent, whether it runs on **Strands Agents SDK**
> (today's only runtime) or the **Claude Agent SDK** (Claude Code packaged as a
> library). The four built-in agents stay on Strands. A Claude-runtime agent
> gets the full set of fleet integrations the Strands runtime has — model via
> Bedrock Mantle + guardrail, Gateway-only Cedar-enforced tools, dispatch
> context, assignment lifecycle + cost accounting, skills, observability — plus
> first-class conversation persistence and a no-lost-work pause contract.
> Depends on: `agent-authoring-spec.md` (capability model),
> `durable-repo-work-and-resume-spec.md` (durability decisions D1–D8, DRAFT).

## 1. Motivation & scope

Today every agent — built-in or dashboard-authored — runs the same shape:
Strands `Agent` inside a `BedrockAgentCoreApp` container on AgentCore Runtime.
The authoring spec deliberately made the agent definition **declarative config
decoupled from the runtime** so an alternative base image could be introduced
"without touching the schema, the deployer, or the UI." This spec introduces
that alternative:

1. **Runtime selection** — a new `runtime` field on the capability row
   (`strands` | `claude-agent-sdk`). New and cloned custom agents pick either;
   built-ins are locked to `strands`.
2. **A second generic base image** (`agents/_claude/`) that implements the SAME
   AgentCore entrypoint contract as `agents/_base/`, but drives the Claude
   Agent SDK (`claude-agent-sdk` on PyPI — bundles the Claude Code CLI binary;
   no Node.js required) instead of Strands.
3. **Build routing** — the shared CodeBuild project reads `runtime` off the
   capability row and builds the matching base image. Everything downstream
   (deployer, registry, router, dispatch payload) is unchanged.
4. **Integration parity** (§5) — every Strands-runtime integration has a named
   Claude-runtime equivalent, with the same enforcement guarantees.
5. **Conversation + work durability** (§6) — session transcripts persist to S3
   every turn on BOTH runtimes (closing the gap that `durable-…-spec.md` D2
   specifies but nothing implements), and in-progress repo work follows the
   same `wip/<assignment_id>` push-clean protocol, so a pause, idle-stop, or
   crash never silently loses ephemeral work.

**Explicitly out of scope (deferred):**
- Migrating any **built-in** agent to the Claude runtime. Built-ins keep their
  per-agent Strands code; `runtime` on a `builtin:true` row is seeded
  `strands` and locked like the rest of built-in config.
- **Plugin-marketplace install** (`capability.plugins`, authoring spec §6.4).
  This runtime makes it feasible (the SDK loads local plugins natively), but
  the field stays reserved/rejected until that spec lands.
- Other runtimes (Codex / Kiro). The `runtime` field is an enum precisely so
  they slot in later; nothing here builds for them.
- Interactive/streaming sessions. Both runtimes stay request/response inside
  AgentCore's invoke window; long-lived interactive sessions are a separate
  roadmap item.

## 2. Runtime decision — why Claude Agent SDK, and what it changes

The Claude Agent SDK is the Claude Code harness as a Python library: the agent
loop, context management/compaction, hooks, subagents, session persistence, and
built-in tools (Read/Write/Edit/Bash/Glob/Grep/WebFetch/…) behind
`query()`/`ClaudeSDKClient`. Two properties matter for us:

- **Native, per-turn session persistence.** `ClaudeAgentOptions.session_store`
  mirrors every transcript line to an external store as it is written, and
  `resume=<session_id>` re-materializes the conversation from that store when
  the local file is absent — exactly the cold-start-resume shape AgentCore's
  ephemeral containers need, and stronger than what we have on Strands today
  (where `S3SessionManager` is specified in the durable-work spec but not yet
  wired). An S3 `SessionStore` reference adapter ships with the SDK.
- **Native skills.** The SDK loads the same `SKILL.md` packages
  (`agentskills.io` spec) our skill store already validates and syncs — the
  `skills` option enables them without new formats.

What it changes vs Strands, and therefore what this spec must govern:

| | Strands runtime (`agents/_base`) | Claude runtime (`agents/_claude`) |
|---|---|---|
| Tool surface | Gateway MCP tools only (no shell, no filesystem tools) | Gateway MCP tools **plus optional built-in tools** (Bash, Read, Write, …) — a new capability class that must be granted, classified, and defaulted OFF (§5.3) |
| Model call | Strands `AnthropicModel` → Mantle endpoint, guardrail headers injected per call | Claude Code CLI subprocess → same Mantle endpoint via `ANTHROPIC_BASE_URL` + short-term bearer token; guardrail via `ANTHROPIC_CUSTOM_HEADERS` (§5.1) |
| Conversation state | None today (fresh `Agent` per invoke) | `session_store` (S3) + `resume`, per-turn (§6.1) |
| Loop control | Single `agent(user_input)` call | `max_turns`, `max_budget_usd`, hooks, `permission_mode` — mapped from the capability's `limits` (§5.6) |

## 3. Concepts

### 3.1 The `runtime` field

```
runtime: "strands" | "claude-agent-sdk"     # NEW on capability# rows
```

| | `strands` | `claude-agent-sdk` |
|---|---|---|
| Base image | `agents/_base/` | `agents/_claude/` (NEW) |
| Built-ins may use | ✅ (locked) | ❌ |
| Custom agents may use | ✅ (default) | ✅ (selected at create/clone) |
| Editable after create | ✅ — an edit flips the field and triggers a rebuild, like a `requirements` edit | same |
| Skills | Strands `AgentSkills` plugin | SDK `skills` option (same S3 packages, same sha256 verify) |
| `tool_grants` | Gateway `Target___tool` ids | Gateway ids **+ builtin-tool ids** (§5.3) |

Missing/absent `runtime` on an existing row reads as `strands` — no migration
required; `render_registry` and the router never see the field.

### 3.2 Same entrypoint, same payload, same registry

`agents/_claude/agent.py` is a `BedrockAgentCoreApp` with an `@app.entrypoint`
handler, containerized identically (port 8080, non-root, arm64, digest-pinned
`python:3.12-slim`). The dispatch payload contract
(`{prompt, session_id, source, source_context, assignment_id}` —
`infra/dispatch/router.py:423-456`) and the completion contract (write status
to the assignments table via `shared.assignment`) are unchanged. The router,
registry, deployer, Cedar trigger authz, and notifier need **zero changes**.

> Fix rolled into this spec: `agents/_base/agent.py:166` reads `instruction` /
> `body` while the router sends `prompt` — custom Strands agents currently rely
> on the fallback path. Both generic runtimes will accept
> `prompt | instruction | body` (in that order) so the contract is explicit.

## 4. The Claude generic base agent (`agents/_claude/`)

One new source dir + Dockerfile, built once per (agent, tag) like `_base`:

**Dockerfile** — mirrors `agents/_base/Dockerfile`: digest-pinned
`python:3.12-slim`, non-root `agent` user **with a writable `HOME`**
(`/app/home` — the bundled CLI persists sessions/config under
`$HOME/.claude`; the current nologin-user pattern would break it), `git`
installed, `_claude/requirements.txt` (=`claude-agent-sdk`, `boto3`,
`bedrock-agentcore`, `aws-bedrock-token-generator`, otel) + the generated
`requirements-extra.txt`, `EXPOSE 8080`,
`CMD ["opentelemetry-instrument", "python", "agent.py"]`. The
`claude-agent-sdk` manylinux **aarch64** wheel bundles the CLI binary, so the
existing native-ARM CodeBuild host needs no change and no Node.js is added to
the image.

**`agent.py` invoke flow** (the Claude analog of `_base/agent.py`):

1. Parse the dispatch payload; require an instruction; resolve
   `assignment_id`/`source`/`source_context` exactly as `_base` does.
2. **Model auth (§5.1):** mint a short-term Mantle bearer token
   (`aws_bedrock_token_generator.provide_token`, runtime-role creds — the same
   no-stored-secret call `shared/bedrock.py:66-76` makes) and assemble the
   subprocess env: `ANTHROPIC_BASE_URL=https://bedrock-mantle.<region>.api.aws/anthropic`,
   `ANTHROPIC_AUTH_TOKEN=<token>`, `ANTHROPIC_MODEL=$BEDROCK_MODEL_ID`,
   `ANTHROPIC_CUSTOM_HEADERS` carrying the guardrail trio
   (`X-Amzn-Bedrock-GuardrailIdentifier/-Version/-Trace`) and the Mantle
   cost-attribution header (`anthropic-workspace-id: $MANTLE_PROJECT_ID`).
   **Fail-closed:** refuse to run if `BEDROCK_GUARDRAIL_ID` is unset (same
   escape hatch env as `build_model`). Tokens are minted per invoke; runs are
   bounded by the 900s invoke window, far inside token lifetime.
3. **Gateway tools (§5.2):** open the existing SigV4 gateway client
   (`shared.tools.gateway`, stamped `agent=AGENT_ID`), enumerate
   `list_tools_sync()`, and wrap each tool in an **in-process SDK MCP server**
   (`create_sdk_mcp_server(name="gateway", tools=[…])`) whose handlers forward
   the call through the SigV4 client. The CLI subprocess never holds a gateway
   credential and cannot reach vendors directly; Cedar at the Gateway remains
   the tool-access authority, unchanged.
4. **Skills:** reuse `_sync_skills_from_s3()` verbatim (hoisted into
   `shared/skills_sync.py` so both bases import it), sync into
   `<cwd>/.claude/skills/`, and pass `skills=[<names>]` from the manifest.
5. **Session (§6.1):** `session_store=S3SessionStore(bucket=$SESSIONS_BUCKET,
   prefix=f"claude/{AGENT_ID}/")`, `session_id=assignment_id` (a UUID, as the
   SDK requires) on first dispatch, `resume=assignment_id` +
   `fork_session=False` on a resume dispatch (§6.2).
6. **Options assembly:** `system_prompt=$SYSTEM_PROMPT` + the same Slack/repo
   dispatch-context block `_base` appends; `cwd=/tmp/work/<assignment_id>`;
   `setting_sources=[]` (SDK isolation — no filesystem settings leak);
   `tools=[…]` and `allowed_tools`/`disallowed_tools` from the grant mapping
   (§5.3); `permission_mode="dontAsk"` with `can_use_tool` denying anything
   ungranted (defense in depth); `max_turns` / `max_budget_usd` from
   `limits` (§5.6); hooks (§5.4, §6.3).
7. Run `query(prompt=…, options=…)` to completion; collect the final
   `ResultMessage`; map `usage` + `total_cost_usd` into
   `complete_assignment(...)` (which already ADD-accumulates across resumed
   segments); on exception `fail_assignment(...)`. Return
   `{"statusCode": 200, "body": <output ≤5000 chars>}` like `_base`.

## 5. Integration parity matrix

Every row below is a launch requirement — a Claude-runtime agent missing any
of these does not ship.

| Integration | Strands mechanism | Claude mechanism |
|---|---|---|
| **5.1 Model via Mantle** | `AnthropicModel` on the Mantle endpoint; bearer from `aws-bedrock-token-generator`; `anthropic-workspace-id` header | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (same generator, minted per invoke, passed only in the subprocess env — never on disk) + `ANTHROPIC_CUSTOM_HEADERS`. `ANTHROPIC_MODEL=$BEDROCK_MODEL_ID` |
| **Guardrail (fail-closed)** | `build_model` raises if `BEDROCK_GUARDRAIL_ID` unset; headers on every model call | Same headers via `ANTHROPIC_CUSTOM_HEADERS` on every CLI model call; adapter refuses to start without the id. Router edge `ApplyGuardrail` is upstream of both runtimes, unchanged |
| **5.2 Gateway-only tools** | Strands `MCPClient` over SigV4 (`mcp_proxy_for_aws`); refuses to boot without `GATEWAY_MCP_URL` | In-process SDK MCP proxy server forwarding to the SAME SigV4 client (the SDK's `McpHttpServerConfig` supports only static headers, so direct connection is impossible by construction — a feature, not a gap). Boot-refusal identical. Tool names surface to the model as `mcp__gateway__<Target___tool>` |
| **Cedar tool policy** | Gateway policy engine filters `list_tools_sync()` per agent id | Identical — the proxy calls the same gateway as the same principal; grants render through the same `fleet_policy` path |
| **Co-repo interceptor / SCM broker** | Gateway REQUEST interceptor + broker token minting | Identical (all calls still traverse the gateway) |
| **Dispatch context** | Prompt-appended `slack_dispatch_block` / repo block | Same block appended to `system_prompt` |
| **Assignment lifecycle** | `complete_assignment` / `fail_assignment` / `extract_usage` | Same functions; usage mapped from `ResultMessage.usage` + `total_cost_usd` (the SDK computes cost natively — recorded alongside our per-token estimate) |
| **Trace refs** | Mined from Strands tool-call transcript | Mined from the SDK message stream's `ToolUseBlock`/`ToolResultBlock` (new `extract_trace_refs_from_sdk_messages` beside the existing extractors) |
| **Skills** | `AgentSkills` plugin over synced S3 packages | SDK `skills` option over the same synced packages (§4.4) |
| **Observability** | `opentelemetry-instrument` + stdout logs → AgentCore OTel | Same wrapper on the adapter process; CLI subprocess stderr surfaced via the `stderr` callback into stdout logs; `CLAUDE_CODE_ENABLE_TELEMETRY` + `OTEL_*` exported so CLI-side metrics join the same pipeline |
| **Response posting** | Model posts via gateway tools; Slack results also fan out server-side from the assignments stream | Identical (both paths are runtime-agnostic) |
| **Weekly security rebuild** | Rebuilds pick up base-image + pip patches | Same, and additionally picks up new `claude-agent-sdk`/CLI releases — the bundled CLI is versioned by the pip dependency, so the rebuild is the patch channel |

### 5.3 Built-in tools are a new grant class — default OFF

The Claude runtime ships built-in tools the fleet has never exposed: `Bash`,
`Read`, `Write`, `Edit`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, subagents.
These do not traverse the Gateway, so Cedar cannot see them — the grant must be
enforced at the runtime boundary. Rules:

- The fleet tool catalog (`fleet_policy`) gains a **`builtin:` namespace**:
  `builtin___Read`, `builtin___Bash`, … classified with the same
  read/write/destructive discipline: `Read`/`Glob`/`Grep` → `read`;
  `Write`/`Edit`/`Bash`/`WebFetch`/`WebSearch` → `write`-class (grantable,
  surfaced with an explicit warning in the picker); nothing in the namespace is
  `destructive`, but **`permission_mode` values other than `dontAsk` and the
  `bypassPermissions` mode are not configuration — they're hardcoded out**.
- A `claude-agent-sdk` capability with **no builtin grants** runs with
  `tools=[]` — Gateway MCP tools only, i.e. exactly the Strands security
  envelope. This is the default for new and cloned agents.
- Granted builtins flow to `ClaudeAgentOptions.tools` (availability) and
  `allowed_tools` (no-prompt execution); everything else is enforced
  ungranted by `tools` omission AND a deny-by-default `can_use_tool` AND a
  `PreToolUse` hook that hard-denies unlisted tool names (three layers,
  because prompts are untrusted input).
- `Bash`/`Write`/`Edit` grants only make sense with the durable-workspace
  story (§6.3); the UI groups them under a "Repo workspace" heading. The
  API rejects `builtin___Bash` etc. on a `strands` capability.
- `scripts/check_gateway_manifest.py` extends to assert the builtin namespace
  stays disjoint/exhaustive against a pinned list for the SDK version.

Container blast radius is unchanged: AgentCore microVM isolation, non-root
user, permissions-boundaried runtime role, no long-lived credentials at rest
(the only secret in the container is the per-invoke Mantle bearer token, which
`workspace_run`-style env scrubbing keeps out of Bash subprocesses).

### 5.4 Hooks are fleet infrastructure, not agent config

The adapter installs fleet hooks that authored config cannot remove:
- `PreToolUse` — builtin-tool deny-by-default (§5.3) and, later, per-tool audit
  events.
- `Stop` — the pause/no-lost-work checkpoint (§6.3).
- `PostToolUse` on gateway tools — trace-ref capture (PR URLs, branch names)
  without waiting for the final message.

### 5.5 AgentCore Memory

The Strands built-ins wire optional `AgentCoreMemoryToolProvider` tools when
`AGENTCORE_MEMORY_ID` is set (no Memory resource is provisioned today —
roadmap). For parity, the Claude adapter exposes the same memory operations as
tools on the in-process SDK MCP server when the env var is set, keyed
identically (`actor_id=AGENT_ID`, `session_id=assignment_id`,
namespace `/agents/<id>/<session>`). Ships behind the same env flag; no new
infra.

### 5.6 Limits mapping

| Capability `limits` | Claude runtime |
|---|---|
| `timeout_minutes` | unchanged — enforced by AgentCore invoke window / router read timeout |
| `daily_token_budget` | pre-dispatch check unchanged; per-run ceiling additionally enforced via `max_budget_usd` derived from the budget × current pricing |
| `max_concurrent` | unchanged (router-side) |
| (new, optional) `max_turns` | `ClaudeAgentOptions.max_turns` — defaulted (e.g. 50) so a tool-loop bug can't burn the window |

## 6. Conversation persistence & no-lost-ephemeral-work

This section makes the durable-work spec's decisions concrete for BOTH
runtimes, so persistence behavior is identical regardless of framework.

### 6.1 One sessions bucket, per-turn persistence on both runtimes

New foundation resource: `sdlc-agent-sessions-${AccountId}-${Stage}` (SSE,
private, lifecycle-expire objects at 30 days to match the assignment TTL).
Injected as `SESSIONS_BUCKET` by the deployer (added to `_base_env`).

- **Claude runtime:** `session_store=S3SessionStore(bucket, prefix="claude/<agent_id>/")`,
  `session_id=assignment_id`. The SDK mirrors every transcript line
  (batched per turn); `resume=assignment_id` re-materializes on any later
  container, warm or cold. `load_timeout_ms` kept at default; `append()`
  failures are non-blocking by SDK design and logged for monitoring.
- **Strands runtime:** wire `S3SessionManager(session_id=assignment_id,
  bucket, prefix="strands/<agent_id>/")` into `agents/_base/agent.py` — this
  implements durable-work D2 for custom agents (built-ins follow in that
  spec's own phasing, not here).

Keying both by `assignment_id` preserves the fleet invariant that the
assignment is the unit of work: the Slack thread binding, the notifier, cost
accumulation, and now the conversation all hang off the same id.

### 6.2 Resume

Resume triggers and routing are exactly `durable-repo-work-and-resume-spec.md`
(thread binding, `awaiting_input → resuming` conditional flip, guardrail on
the reply). Only step 5 differs per runtime:

- **Claude:** construct options with `resume=assignment_id` and pass the
  human's reply as the new `prompt`. No interrupt-payload reconstruction is
  needed — resuming with a new user message is the SDK's native model, which
  simplifies the Strands `interruptResponse` dance to a plain re-invoke.
- **Strands:** as specced (session restore + `interruptResponse`).

The router's resume dispatch therefore adds one payload field:
`"resume": true` — each runtime interprets it natively.

### 6.3 No lost ephemeral work (the pause contract)

Loss boundary and mechanics follow durable-work D1/D3/D7 unchanged — disk is
scratch; durable workspace = `wip/<assignment_id>` branches via
`shared/tools/workspace.py` (already built; this spec wires it):

- The workspace `@tool`s (`clone_repo`, `workspace_run`, `commit_and_push`)
  are exposed to Claude-runtime agents as SDK MCP tools on the in-process
  server (NOT via raw `Bash` — the vendor-token minting and env scrubbing live
  in the tool). `Bash` grants are for build/test loops inside the already-
  cloned workspace.
- **Pause protocol, Claude flavor:** the `ask_user` SDK MCP tool triggers the
  load-bearing sequence: `push_all_clean()` (verify remote sha == local sha,
  fail loud on failure per D7) → record `workspace_snapshot` + the question on
  the assignment row → flip `status=awaiting_input` → end the turn. A fleet
  `Stop` hook re-verifies the tree is clean whenever an `awaiting_input` flip
  happened this run — a belt-and-suspenders check that no dirty state can
  slip through the commit point.
- **Involuntary stop:** conversation is already durable per-turn (§6.1);
  workspace loses only edits since the last commit — same stated loss boundary
  as the durable-work spec, now with the conversation half actually
  implemented. Optionally, `enable_file_checkpointing` stays OFF (checkpoints
  are local scratch and would be lost anyway — misleading durability).
- Cost accounting across resumed segments already ADD-accumulates
  (`shared/assignment.py`); `ResultMessage.usage` per segment feeds the same
  path.

## 7. Build & deploy deltas

The discriminator today is structural (`agents/<name>/Dockerfile` exists →
built-in). It gains one lookup it already performs anyway:

1. **Buildspec** (`infra/foundation/template.yaml` inline): the custom-agent
   branch already runs `aws dynamodb get-item` for `requirements`; it now also
   reads `runtime` from the same item and selects the Dockerfile:
   `strands`/absent → `agents/_base/Dockerfile`; `claude-agent-sdk` →
   `agents/_claude/Dockerfile`. `gen_requirements.py` output is layered onto
   whichever base. Built-in branch unchanged.
2. **ECR / tags / arm64:** unchanged — the aarch64 SDK wheel installs natively
   on the ARM CodeBuild host; no buildx.
3. **Deployer:** unchanged flow. `_base_env` additionally injects
   `SESSIONS_BUCKET` (both runtimes), and the runtime role gains scoped
   read/write on `sdlc-agent-sessions-*/{claude,strands}/<agent_id>/*` plus
   the existing skills-bucket read. The permissions boundary is widened to
   allow the sessions bucket ARN.
4. **Weekly rebuilder:** unchanged (it re-StartBuilds; the buildspec re-reads
   `runtime`).
5. **`deploy_fleet.py`:** seeds built-ins with `runtime: "strands"`; uploads
   `agents/_claude/` in `source.zip` (already zips all of `agents/`).

## 8. Schema, API & UI deltas

### 8.1 `capability#` row

```
runtime: "strands" | "claude-agent-sdk"   # NEW — absent ⇒ "strands";
                                          #       builtin rows locked "strands"
tool_grants: [str]                        # now accepts builtin___* ids ONLY
                                          #       when runtime == claude-agent-sdk
limits.max_turns: int                     # NEW, optional (claude runtime)
```

### 8.2 Admin API

- `POST /admin/capabilities` — accepts `runtime` on custom rows; rejects it on
  `builtin` rows (400, same fixed-config rule as today); rejects
  `builtin___*` grants when `runtime != "claude-agent-sdk"`; rejects unknown
  builtin ids against the catalog. A `runtime` **edit** is treated like a
  `requirements` edit: novel-config check → approval gate if ON → rebuild.
- `POST /admin/capabilities/{id}/clone` — copies `runtime` from the source;
  the create form lets the admin change it before enabling (a clone of a
  Strands built-in can therefore become a Claude agent — this is the
  "clone onto the new runtime" path).
- `GET /admin/tool-catalog` — response gains the `builtin` connector group
  with per-tool class tags, plus a `runtimes` applicability field per tool so
  the UI can grey builtins out for Strands agents.

### 8.3 UI (`CapabilitiesPanel`)

- **New/Clone editor:** a "Runtime" selector (radio: *Strands Agents* —
  default; *Claude Agent SDK*) with a one-line description of what changes
  (built-in tool availability, session persistence). Hidden/read-only on
  `edit` of built-ins.
- **Tool grants picker:** builtin group appears only when runtime =
  Claude Agent SDK; `write`-class builtins carry the warning treatment.
- **List rows:** a small runtime badge next to the agent id.

### 8.4 Approval gate interaction

Runtime selection is config, and the Claude runtime with builtin grants is a
capability-envelope expansion — so under `RequireAgentApproval=true`, a
create/clone/edit that (a) selects `claude-agent-sdk` for the first time or
(b) adds any `builtin___*` grant lands `pending_review` and needs the
second-admin approval, exactly like novel `requirements`/`skills`.

## 9. Security model

1. **Gateway-only preserved by construction (T-4 posture).** The CLI cannot
   SigV4-sign; the only route to any vendor is the in-process proxy → the
   Gateway → Cedar → interceptor → broker. No new credential exists at rest;
   the per-invoke Mantle bearer lives only in the subprocess env.
2. **Built-in tools are a real expansion — gated three ways** (§5.3): `tools`
   omission, `can_use_tool` deny-by-default, `PreToolUse` hard deny. Default
   is zero builtins; granting any is second-admin-approved (§8.4).
3. **Bash + PUBLIC egress is the biggest new surface.** A prompt-injected
   agent with `Bash` could attempt exfiltration of anything in the container.
   Mitigations: guardrail on every model call + router edge check (unchanged);
   env scrubbing of `AWS_/GITHUB_/SLACK_/ASANA_` prefixes for Bash subprocesses
   (reusing the `workspace_run` scrub list); no secrets at rest; grants
   reviewed. Residual risk accepted and recorded in the threat model
   (new T-id) — same acceptance we already make for `workspace_run`.
4. **Settings isolation.** `setting_sources=[]` and `strict_mcp_config=True`
   so nothing on the filesystem (including a skill that writes
   `.claude/settings.json` or `.mcp.json`) can alter permissions or register
   MCP servers. Skills remain instruction-only (prompt-injection surface, as
   the authoring spec §3.4 states — unchanged analysis).
5. **`bypassPermissions` / `acceptEdits` are unreachable** — `permission_mode`
   is not authored config; the adapter hardcodes `dontAsk`.
6. **Sessions bucket** holds full conversation transcripts (may contain repo
   content): SSE, private, per-agent prefix scoping on the runtime role,
   30-day lifecycle. Added to the threat model data-inventory.
7. **Supply chain:** `claude-agent-sdk` is pinned in
   `agents/_claude/requirements.txt` like every base dep; the package-index
   allowlist (§7.1 of the authoring spec) governs per-agent extras unchanged;
   the weekly rebuild is the patch cadence for the bundled CLI.

> **Security review required before implementing:** built-in tool exposure,
> the sessions bucket, and the Bash egress analysis must be reflected in
> `docs/threat-model.md` before code ships (same rule as authoring spec §7).

## 10. Decisions (proposed) + remaining questions

**Proposed (this spec):**
- **D1 Runtime is a per-capability enum field**, not inferred structure;
  absent ⇒ `strands`; built-ins locked.
- **D2 Second generic base image** realizes the seam; entrypoint/payload/
  registry contracts identical; router/deployer untouched.
- **D3 Gateway access via in-process SDK MCP proxy** — the SigV4 client stays
  the single egress; Cedar unchanged.
- **D4 Model via Mantle env-injection** (base URL + per-invoke bearer +
  custom headers), guardrail fail-closed.
- **D5 Conversation durability on BOTH runtimes**, S3-backed, keyed by
  `assignment_id`, per-turn; one bucket, per-framework/agent prefixes.
- **D6 Builtin tools default OFF**, granted per-tool through the existing
  catalog + approval machinery.
- **D7 Resume payload flag** (`"resume": true`) interpreted natively per
  runtime.

**Remaining questions (need an answer before P2):**
- **Q1 — Builtin grant ceiling.** Should `Bash`/`Write` be grantable at all in
  v1, or should P1 ship read-only builtins (`Read`/`Glob`/`Grep`) and defer
  the write set to the durable-workspace wiring (§6.3)? Recommendation:
  read-only in P1; write set lands with P3 so it never exists without the
  push-clean protocol.
- **Q2 — WebSearch/WebFetch.** These reach the public internet directly
  (not via Gateway). Allow as grantable `write`-class, or exclude from the
  catalog entirely in v1? Recommendation: exclude in v1 (the Researcher's
  Tavily-shaped needs are a Gateway-target decision per authoring spec §3.3).
- **Q3 — Subagents.** `ClaudeAgentOptions.agents` (programmatic subagents) is
  powerful but multiplies the loop-control surface. Recommendation: not
  exposed as authored config in v1; the adapter may use it internally later.
- **Q4 — Mantle/Claude Code compatibility check.** The CLI is assumed to work
  against the Mantle Messages endpoint via `ANTHROPIC_BASE_URL`; P1 starts
  with a spike that proves model calls, guardrail headers, and cost headers
  end-to-end before anything else is built.

## 11. Phasing

- **P1 — Runtime seam + adapter spike:** `runtime` field (schema, API
  validation, clone copy-through, seeding), buildspec routing,
  `agents/_claude/` image + adapter with model auth (Q4 spike), gateway MCP
  proxy, skills, assignment lifecycle, dispatch context. Zero builtin tools
  (`tools=[]`). UI runtime selector + badge. Threat-model update.
- **P2 — Sessions & resume:** sessions bucket + role/boundary changes;
  `session_store` wiring (Claude) + `S3SessionManager` wiring
  (`_base`, custom agents); router `resume` flag + thread-binding resume path
  (shared with the durable-work spec's Phase 2 — coordinate, don't duplicate).
- **P3 — Builtin tool grants + workspace:** builtin catalog namespace +
  classification + picker UI + approval-gate hook (read-only set first, per
  Q1); workspace tools on the SDK MCP server; `ask_user` pause protocol +
  `Stop` hook; env scrubbing.
- **P4 — Parity extras:** AgentCore Memory tools behind the env flag;
  trace-ref extraction from SDK messages; `max_turns` limit; CLI OTel
  export tuning.

Each phase ships behind tests + the green-sweep discipline; docs
(`03-design`, `aws-deploy`, `threat-model`, `roadmap.md` Document Index)
updated per phase.

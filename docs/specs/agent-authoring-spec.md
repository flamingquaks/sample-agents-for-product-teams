# Agent Authoring & Lifecycle (v2.2) — SPEC / DESIGN

> **Status: APPROVED — implementing.** This spec extends the
> capability model (`docs/03-design-agent-fleet.md §4.4`, `docs/aws-deploy.md`,
> `infra/dashboard/config_store.py`) so admins can **author config-driven agents
> from the dashboard** instead of only registering repo-committed ones. It
> supersedes the "an `agent_id` must match an `agents/<id>/` source dir"
> constraint for *custom* agents.

## 1. Motivation & scope

Today every agent is code in `agents/<name>/` (its own `agent.py`, `tools/`,
`prompts.py`, `requirements.txt`, and a Dockerfile that is **byte-identical
across the four except the copied directory name**). The dashboard only
*registers + builds + deploys* an agent whose source already exists; it can't
create one. This spec adds an authoring layer with four capabilities:

1. **System (built-in) agents** — the 4 existing agents become seeded, fixed,
   **enable/disable-only** capabilities that cannot be deleted or edited.
2. **Config-driven custom agents** — a new agent is a `capability#` row (prompt +
   dependencies + connector allowlist + skills), built on a **generic base
   image**; no code checkin, no per-agent Dockerfile.
3. **Clone** — copy a built-in into a new, editable custom agent as a starting
   point.
4. **Skills** — upload `SKILL.md` packages (`.md` or `.zip`), stored in S3,
   loaded natively by Strands `AgentSkills`.

**Explicitly out of scope (deferred):**
- **Plugin-marketplace install** (`/plugin marketplace add …`) — deferred to the
  future Claude Code runtime expansion. We reserve a config seam (§6.4) but build
  nothing.
- **Bespoke-integration tools** (new SDK + new secret, e.g. the Researcher's
  Tavily `web_search`) — cannot be authored from a form (§3.3); these remain
  code-defined built-ins or a future shared Gateway target.

## 2. Runtime decision — stay on Strands, behind a swappable seam

Config-driven agents run on **Strands Agents SDK** (same as the built-ins), for
one decisive reason: **Strands supports skills natively** via the `AgentSkills`
plugin, and it uses the **same `agentskills.io` `SKILL.md` spec** that Claude
Code / `anthropics/skills` publish against. So we get skill upload *and* future
Anthropic-published skills with no runtime change.

**Future flexibility (Codex / Kiro / Claude Code).** The agent definition is
**declarative config** (prompt, deps, connectors, skills) stored in DynamoDB +
S3, decoupled from the runtime. A future runtime swap only needs its own loader
that reads the same config and points its skill mechanism at the same S3-stored
`SKILL.md` packages. The generic `agent.py` (§4) is the ONLY runtime-coupled
piece; it sits behind a thin `RUNTIME` seam so an alternative base image can be
introduced without touching the schema, the deployer, or the UI.

## 3. Concepts

### 3.1 Built-in vs custom capabilities

| | Built-in (system) | Custom (authored) |
|---|---|---|
| `builtin` | `true` | `false` |
| Source of definition | repo `agents/<id>/` (code) | the `capability#` row (config) |
| Image | its own Dockerfile | the **generic base image** |
| Admin edits | **none** — config is fixed | full (prompt/deps/connectors/skills) |
| Enable/Disable | ✅ | ✅ |
| Delete | ❌ refused (409) | ✅ |
| Clone → new custom | ✅ (source template) | ✅ |
| Seeded by | `deploy_fleet.py` | created via dashboard |

### 3.2 What a config-driven agent CAN compose

- **`system_prompt`** — the agent's context/instructions (was `prompts.py`).
- **`requirements`** — a list of pip specifiers → the build **generates
  `requirements.txt`** (§5). Subject to the package-index allowlist (§7).
- **`tool_grants`** — a **per-tool** allowlist, not a coarse "connector"
  toggle (§3.5). Each granted tool is a live Gateway `Target___tool` id, and the
  agent's grant is the authored subset of what the Gateway exposes.
- **`skills`** — `SKILL.md` packages the agent loads (§6).

### 3.3 What it CANNOT (by design, not by omission)

A config-driven agent cannot introduce **new bespoke Python tools with new
dependencies + secrets** (the Tavily `web_search` shape). Reason: that is
arbitrary code + credential injection from an admin form, which no amount of
config can make safe. Such an integration must be added as a **shared Gateway
target** (a code checkin, reviewed) and then it becomes a selectable
`connector` for everyone. This keeps the "author from a form" surface to
prompt + already-trusted connectors + skills.

### 3.4 Skills do NOT widen tool access

`SKILL.md` frontmatter has an `allowed-tools` field, but per the Strands docs it
is **"currently informational" — not enforced at runtime.** So a skill can
*suggest* tools but cannot grant them. The **Gateway Cedar policy + the
capability's `tool_grants` (§3.5) remain the only tool-access authority.** A
skill is instruction + optional bundled resource files, never a privilege grant.
(Security note: a skill IS injected instruction, so it's a prompt-injection
surface — see §7.)

### 3.5 Tool grants — per-tool, read/write/destructive classified

Tool access is granted **per individual tool, not per connector**, because the
fleet splits **reads from writes** and forbids **destructive** operations
outright. This is not new: it mirrors the model already enforced today in
`infra/dashboard/fleet_policy.py` — `AGENT_TOOL_GRANTS` (the per-agent allowlist,
in `Target___tool` shape), `WRITE_TOOLS` (forbidden unless the call's target repo
is allowed), and `DESTRUCTIVE_TOOLS` (forbidden unconditionally — the "agents
NEVER close issues / merge PRs / delete tasks" rule). The built-in `cedar/*.cedar`
files already section their grants into `// read` and `// write` comment blocks.

**Tool catalog + classification.** Every tool the Gateway exposes
(`tools/list`) is classified as `read` | `write` | `destructive`. Today
`fleet_policy` only names the non-read classes (`WRITE_TOOLS`,
`DESTRUCTIVE_TOOLS`); reads are implicit ("everything else"). This spec promotes
the classification into one **fleet tool catalog** in `fleet_policy`, adding an
explicit **`READ_TOOLS`** so the authoring UI can positively render a tool as a
read and the API can validate a grant against a known set (an implicit "not in
write/destructive" is not enough to *offer* a tool in a picker).

`READ_TOOLS` is seeded from the read tools the built-ins are already granted
(`AGENT_TOOL_GRANTS` + the `// read` blocks in `cedar/*.cedar`), e.g. GitHub:
`get_issue`, `list_issues`, `get_pull_request`, `list_pull_requests`,
`get_pull_request_diff`, `list_pull_request_files`, `list_commits`,
`list_milestones`, `get_file_contents`, `search_code`; Asana: `get_task`,
`list_tasks`, `list_projects`, `get_project_status`, `search`,
`get_task_stories`. The three lists are kept **disjoint and exhaustive** over the
live manifest — `scripts/check_gateway_manifest.py` fails if a manifest tool is
unclassified (so a newly-exposed tool can't silently become grantable without a
read/write/destructive decision) or if a tool appears in two classes.

Classification drives enforcement, unchanged in spirit: `read` → grantable
freely; `write` → grantable but bounded by the per-repo write forbid
(`sdlc_allowed_repos`); `destructive` → never grantable, unconditional forbid.

**A capability's `tool_grants`** is the authored subset of catalog tools, each an
`AGENT_TOOL_GRANTS`-shape id. Rules, enforced at the API boundary and re-enforced
at policy render:
- **`destructive` tools can never be granted** — the API rejects them and the
  fleet forbid wins regardless (defense in depth; matches CLAUDE.md).
- **`write` tools** are granted but stay bounded by the existing per-repo write
  restriction (`sdlc_allowed_repos`) — a write is still only permitted on an
  onboarded/co-repo-approved repo.
- **`read` tools** are grantable freely.
- The authored grants flow into the **same enforced policy** the built-ins use:
  `fleet_policy` gains a data-driven `AGENT_TOOL_GRANTS` (read a capability's
  `tool_grants` rather than only the hardcoded built-in map), so custom and
  built-in agents share one enforcement path. **The read/write split lives in
  Cedar/the Gateway, not the runtime** — the agent can only call what its grant +
  the fleet forbids permit.

This is the concrete answer to "connectors": the UI groups tools **by connector
(target) for display**, but the unit of grant — and of the read/write split — is
the **individual tool**.

## 4. The generic base agent (`agents/_base/`)

A single new source dir + Dockerfile, built once as the base image every custom
agent runs. It reproduces the built-ins' Dockerfile (same pinned digest,
non-root user, `opentelemetry-instrument python agent.py`) and its generic
`agent.py`:

1. reads `AGENT_ID`, `SYSTEM_PROMPT`, `SKILLS_DIR` from the runtime env / a
   mounted config doc (written by the deployer, §5);
2. builds the model via `shared.bedrock.build_model` (guardrail-gated, as today);
3. opens the **one Gateway client** (`shared.tools.gateway`) and takes the
   gateway tools — the per-tool `tool_grants` are enforced at the Gateway per
   agent-id (§3.5), so the generic agent needs no per-tool code; it simply gets
   whatever the policy permits for its id;
4. wires `AgentSkills(skills=[<resolved skill dirs>])` from `SKILLS_DIR`;
5. runs the same assignment/complete/fail lifecycle as the built-ins.

Because the base agent has **no hardcoded tool imports**, its `requirements.txt`
is the common set (`strands-agents`, `bedrock-agentcore`, the memory/skills
providers, `boto3`, otel). Per-agent `requirements` (§5) are layered on at build.

## 5. Requirements as config → generated `requirements.txt`

- The `capability#` row carries `requirements: [str, ...]` (pip specifiers).
- The build **generates** `requirements.txt` from `base_requirements +
  capability.requirements`, then the existing buildspec `pip install -r` step
  runs unchanged. (Mechanically: the deployer/build writes the file into the
  build context, or passes the list as a build env the buildspec materializes —
  decided at implementation.)
- **Built-in agents**: `requirements` is seeded and **locked** (fixed config).
- **Custom agents**: `requirements` is editable but constrained by the index
  allowlist (§7): specifiers only, no `--index-url`/`-e`/URLs/VCS refs.

## 6. Skills

### 6.1 Storage — S3-backed

A new bucket `sdlc-agent-skills-${AccountId}-${Stage}` (SSE, versioned, private).
Each skill package is stored under `skills/<capability-or-shared>/<skill-name>/`
as its expanded `SKILL.md` tree. The `capability#` row references skills by
key + a content hash (so a rebuild is reproducible and the weekly rebuild picks
up the same skills).

```
capability.skills = [
  { name, s3_prefix, sha256, scope: "capability" | "shared" }
]
```

### 6.2 Upload — `.md` and `.zip` (adapter)

New admin route `POST /admin/skills` (multipart or presigned-PUT + register):

- **`.md`** — a single `SKILL.md`; validated against the agentskills.io spec
  (required `name` [lowercase/hyphen, ≤64], `description`); stored as
  `<name>/SKILL.md`.
- **`.zip`** — a full skill package (`SKILL.md` + optional `scripts/`,
  `references/`, `assets/`). The **unpack adapter** (§6.3) validates and expands
  it to the S3 tree; Strands has no native `.zip` source, so we always hand
  `AgentSkills` an expanded **directory**, never a zip. At runtime the deployer
  syncs the referenced S3 prefixes into the agent's `SKILLS_DIR`.

### 6.3 The `.zip` unpack adapter (security-critical)

Runs in an isolated Lambda, not the build:
- reject zip-slip (entries escaping the root), symlinks, and oversize
  (per-file + total caps);
- require a top-level `SKILL.md`; validate its frontmatter;
- **do not execute anything** — `scripts/` files are stored as data. They only
  run if the agent's own tools (`shell`/`file_read`) invoke them at runtime,
  which is gated by the per-tool grants + guardrail like any other action;
- record `sha256` of the normalized tree.

### 6.4 Deferred: marketplace install (seam only)

`capability.plugins` is **reserved** in the schema and **rejected by the admin
API** for now. When the Claude Code runtime lands, "install `anthropics/skills`"
becomes: fetch the named package → run it through the SAME §6.3 validator →
store in the skills bucket → reference it like any uploaded skill. No new
concept, just a fetch source. Documented here so the schema doesn't churn later.

## 7. Security model (first-class, not an afterthought)

Config-driven agents turn **admin-supplied input into code/instruction that runs
in the build and the production agent runtime**. This collides with v2's posture
(trusted agent *code*, untrusted *behavior*). Controls:

1. **Package-index allowlist.** `requirements` entries are validated to be plain
   PyPI specifiers (`name[extras]<op>version`). No `--index-url`, `--extra-index-url`,
   `-e`, direct URLs, or VCS refs — closes the typo-squat / attacker-index
   supply-chain vector. Build runs against a pinned index.
2. **Skill validation** (§6.3) — zip-slip/symlink/size guards; no install hooks
   executed at upload; frontmatter schema-checked.
3. **Skills are prompt-injection surface.** Because a skill is injected
   instruction, an uploaded skill is treated like admin-authored prompt content;
   the existing per-model **guardrail still runs on every call**, and the
   **Gateway Cedar policy still bounds every tool call** — a skill cannot escape
   either.
4. **Blast radius unchanged.** The build role still only pushes to ECR; the
   runtime role is still permissions-boundaried under `/sdlc-agents/capabilities/`.
   Arbitrary deps widen what runs in the *build*, so the allowlist (#1) is what
   keeps that contained.
5. **Approval gate (deploy-time parameter, default ON).** A custom agent with
   novel `requirements` or `skills` goes `pending_review` and requires a **second
   admin** to approve before the build starts — mirroring the fleet-wide
   propose→approve pattern. Whether the gate is on is a **CloudFormation/SAM
   parameter** `RequireAgentApproval` (default `true`), NOT a runtime toggle — so
   relaxing it requires **deploy rights** (a strictly higher bar than an admin API
   token) and is an auditable infra change, sidestepping "who can flip the button
   / does flipping it need two admins." It flows to the admin Lambda env; the API
   reads it from env. When off, a create goes straight to build. Built-in
   enable/disable never needs approval (config is fixed).
6. **Destructive tools ungrantable (§3.5).** The API rejects any `tool_grant`
   classified `destructive`; the fleet `DESTRUCTIVE_TOOLS` forbid wins regardless.
   Writes stay bounded by the per-repo write restriction. A config-authored agent
   cannot exceed the read/write/destructive envelope the built-ins live in.
7. **Reserved env unchanged.** `RESERVED_ENV_KEYS` (guardrail id/version, gateway
   URL) remain un-overridable; base env wins on merge.

> **Security review required before implementing §7:** the package-index
> allowlist, skill-upload handling, and (later) marketplace fetch touch
> supply-chain + arbitrary-code domains. These controls are captured in the
> living threat model (`docs/threat-model.md`) and must be reflected there before
> the code ships.

## 8. Schema, API & UI deltas

### 8.1 `capability#` row — new fields

```
builtin:        bool            # NEW — seeded true for the 4; false otherwise
base:           "generic"       # NEW — which base image (only value for now)
system_prompt:  str             # NEW — custom agents; empty/locked for built-ins
requirements:   [str]           # NEW — pip specifiers (allowlisted §7)
tool_grants:    [str]           # NEW — per-TOOL allowlist, Target___tool ids (§3.5);
                                #       destructive-classified tools rejected
skills:         [ {name, s3_prefix, sha256, scope} ]   # NEW
review_status:  "approved" | "pending_review"  # NEW — set when the approval
                                #       gate (§7.5) is ON; "approved" when OFF
plugins:        RESERVED        # rejected by API until Claude Code (§6.4)
```
Existing `enabled`/`status` semantics unchanged; `CAP_DISABLED` + the
`render_registry` skip already implement **de-route on disable**.

The approval gate is a **deploy-time parameter** (`RequireAgentApproval`, §7.5),
not a `settings` row field — nothing runtime-mutable governs it.

### 8.2 Admin API

- `POST /admin/capabilities` — accepts new fields for **custom** agents; for a
  `builtin` row, rejects any change except `enabled` (400). Rejects any
  `tool_grant` classified `destructive` (§3.5). When `require_agent_approval` is
  ON, a create/edit with new deps/skills lands `review_status=pending_review`
  and does NOT start a build until approved.
- `POST /admin/capabilities/{id}/approve` — **NEW.** Second-admin approval →
  `review_status=approved`, then starts the build. Only meaningful when the gate
  is on; the caller must differ from the author (enforced) .
- `DELETE /admin/capabilities/{id}` — for a **custom** agent this **destroys**
  it: removes the row, de-routes it, and (P2+) tears down its runtime + role +
  ECR image and its capability-scoped skills. For a **`builtin`** agent → **409**
  (extends the current handler); a built-in is disabled, never deleted.
- `POST /admin/capabilities/{id}/clone` — **NEW.** Copies a capability's
  declarative config into a new `pending`, `builtin:false` row (strips
  deploy-state + `builtin`; `review_status=pending_review` if the gate is on);
  admin then edits + enables. This is how you "start from a system agent."
- `POST /admin/skills`, `GET /admin/skills`, `DELETE /admin/skills/{key}` — NEW.

### 8.3 Seeding (built-ins)

`deploy_fleet.py`, when it uploads the source zip, **idempotently upserts** a
`capability#` row for each of `workitems`, `researcher`, `docwriter`, `adr` with
`builtin:true`, their fixed config, `enabled:false`, `status:disabled` — so they
appear as built-in, ready to enable. Seeding must **preserve deploy state /
status** of an already-active built-in (reuse `put_capability`'s edit discipline)
so a redeploy never knocks a live agent out of the registry.

### 8.4 UI (`CapabilitiesPanel`)

- **Built-in rows**: an **Enable/Disable** toggle (no Delete); config shown
  read-only. Disable clearly labeled **"disabled — de-routed (runtime left
  running; not torn down)"**.
- **Custom rows**: full edit form — prompt, requirements, **tool grants** (a
  checklist of the fleet tool catalog **grouped by connector**, each tool tagged
  `read`/`write`, destructive tools shown disabled/ungrantable — §3.5), skills
  upload/attach — plus Enable/Disable and **Delete** (confirm-guarded; **destroys**
  the custom agent and its resources).
- **Clone** action on any row (incl. built-ins) → opens the create form
  pre-filled from that agent's config.
- When the approval gate is on, a `pending_review` agent shows an **Approve**
  action (visible to a different admin than the author).

## 9. Decisions (resolved) + remaining questions

**Resolved:**
1. **Approval gate:** ✅ **configurable** — a `require_agent_approval` fleet
   setting, default ON; an admin can disable it (§7.5, §8.1).
2. **Disable = de-route only** (runtime left running); full teardown stays a
   separate roadmap item; UI labels it (§8.4). **Delete of a *custom* agent
   destroys it** (row + runtime + role + image + capability-scoped skills, §8.2).
   Built-ins can't be deleted (409).
3. **Built-in `requirements` (and all built-in config) locked** — enable/disable
   only; patching is the weekly rebuild against the pinned base + fixed deps.
4. **Tool granularity:** ✅ **per individual tool**, read/write/destructive
   classified (§3.5) — the fleet is splitting reads vs writes, so the grant unit
   is the tool, not the connector. Destructive tools are ungrantable.

**Resolved (this round):**
- **9.1 Approval-gate control:** ✅ **deploy-time parameter** `RequireAgentApproval`
  (default `true`), not a runtime toggle — governed by deploy rights, auditable,
  no "who-can-flip-it" problem (§7.5, §8.1).
- **9.2 Tool catalog:** ✅ lives in **`fleet_policy`** — promote the classification
  into one catalog with **`READ_TOOLS` + `WRITE_TOOLS` + `DESTRUCTIVE_TOOLS`**
  (see §3.5), reconciled against the live manifest by
  `scripts/check_gateway_manifest.py`.

## 10. Phasing (once decisions land)

- **P1 — Built-ins + lifecycle ✅ DONE:** `builtin` flag, idempotent seeding in
  `deploy_fleet.py`, delete-guard (409 for built-ins), enable/disable UI. Small,
  no runtime change.
- **P2 — Data-driven tool grants + read/write split ✅ DONE:** tool catalog
  (read/write/destructive) in `fleet_policy`, `AGENT_TOOL_GRANTS` reads a
  capability's `tool_grants`, built-in + custom render through one policy,
  `check_gateway_manifest.py` reconciliation, UI tool checklist grouped by
  connector.
- **P3 — Generic base agent + requirements-as-config + clone + custom delete
  ✅ DONE:** `agents/_base/` generic `agent.py`; the buildspec routes custom
  agents to the base image and generates `requirements-extra.txt` from the
  capability row via `gen_requirements.py` (re-validated before pip); clone +
  edit in the UI; custom-agent **destroy** teardown (row + runtime + role + image
  + capability-scoped skills) on the deployer, async-invoked by the de-routing
  admin API.
- **P4 — Skills ✅ DONE:** S3 bucket, upload routes, `.zip` adapter in an
  **isolated** `capability-skill-unpacker` Lambda, `AgentSkills` wiring with a
  startup S3 sync + normalized-tree sha256 verify, UI upload/attach.
- **P5 — Approval gate ✅ DONE:** `RequireAgentApproval` deploy-time parameter +
  `pending_review` + second-admin approve route/UI. Novelty is measured against
  the last-approved baseline.
- **P6 (deferred):** marketplace fetch on the Claude Code runtime — reuses the P4
  validator + the reserved `plugins` field.

Each phase ships behind tests + the existing green-sweep discipline; docs
(`03-design`, `aws-deploy`, `threat-model`) updated per phase.

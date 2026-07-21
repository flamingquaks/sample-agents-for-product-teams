# Technical Design Document
## Autonomous PDLC Agent Fleet

**Document Version:** 2.1
**Date:** July 2026
**Status:** Describes the v1 fleet as it ships. Sections marked *Roadmap* are planned, not implemented.

---

## 1. System Overview

The PDLC Agent Fleet is a multi-agent system on **Amazon Bedrock AgentCore Runtime** that automates project management, business analysis, documentation, and architecture-decision linking across **GitHub** and **Asana**. It consists of four agents, a cross-platform routing layer, and Cedar policies that bound what each agent is allowed to do.

### 1.1 Design Principles

**Narrow agents, focused prompts.** Each agent does one role well. The system prompt lives alongside the agent code (`agents/<name>/prompts.py`) and is versioned with it. No generalist agent.

**Deterministic tools, LLM orchestration.** Custom `@tool` functions are structured task prompts or deterministic helpers (fetch an issue, format a comment, validate input). The LLM decides what to call and in what order. Business logic lives in prompts, not in code that pretends to be an agent.

**Safe by default.** Cedar policies forbid destructive operations (merge PRs, close issues, delete tasks). Every agent ships with a per-agent Cedar file in `cedar/<agent>.cedar`. The policy file is the source of truth; no IAM trickery replaces it.

**Platform-native UX.** Users interact with agents by `@mention` in the tool they already use. Results post back to the originating platform in a format that platform understands (GitHub markdown, Asana comment text).

**One path, no forks.** No feature flags, no "v1 vs v2" branches in the code. The shipping fleet is one path; planned work is clearly marked as roadmap.

### 1.2 Component Map

The rendered diagram is [`docs/assets/architecture.mmd`](assets/architecture.mmd) (Mermaid). ASCII overview:

```
┌─────────────────────────────────────────────────────────────────┐
│                     EXTERNAL PLATFORMS                           │
│              GitHub (Issues, PRs)  │   Asana                    │
└──────────────┬──────────────────────────┴──────┬────────────────┘
               │  @mention events                │
               ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     DISPATCH LAYER                               │
│                                                                  │
│  GitHub App webhook Lambda          Asana Webhook Lambda         │
│  (github-webhook-${STAGE})          (asana-webhook-${STAGE})     │
│  HMAC X-Hub-Signature-256           HMAC signature              │
│         │                                    │                   │
│         └────────────────┬───────────────────┘                   │
│                          ▼  async invoke (verified events only)  │
│          Dispatch Router Lambda (dispatch-router-${STAGE})       │
│          ┌─────────────────────────────┐                         │
│          │ • Edge guardrail (apply_guardrail)                    │
│          │ • Parse @mention + aliases   │                         │
│          │ • Check authorization        │                         │
│          │ • Track in DynamoDB          │                         │
│          │ • Invoke AgentCore Runtime   │                         │
│          └─────────────────────────────┘                         │
│          Config: SSM (rendered from fleet-config capability rows) │
│          State: DynamoDB (dispatch-assignments-${STAGE})         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────┼──────────────────────────────────────┐
│          AGENT LAYER     │    (AgentCore Runtime)                │
│                          ▼                                       │
│  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌────────┐            │
│  │ workitems │ │ researcher │ │ docwriter │ │  adr   │            │
│  │ Strands  │ │ Strands    │ │ Strands  │ │Strands │            │
│  └──────────┘ └────────────┘ └──────────┘ └────────┘            │
│                                                                  │
│  Each agent: Strands SDK + BedrockAgentCoreApp in a container   │
│  Model: Claude Sonnet 5 via Bedrock Mantle (bearer token +      │
│         guardrail headers; fleet OpenAI-Project)                │
│  Tool access: AgentCore Gateway only (SigV4) — no direct MCP    │
└──────────────────────────┬──────────────────────────────────────┘
                           │  all MCP tool calls (SigV4)
┌──────────────────────────┼──────────────────────────────────────┐
│                  TOOLS via AgentCore Gateway                     │
│         Cedar policy engine + REQUEST interceptor                │
│                           ▼                                       │
│   Asana MCP (AsanaTarget)             GitHub SCM broker target   │
│   https://mcp.asana.com/v2/mcp        (scm_broker.py — mints     │
│   (OAuth)                              per-owner App tokens)     │
│                                                                  │
│   Tavily web search (Researcher only, via the gateway)          │
└─────────────────────────────────────────────────────────────────┘
```

**What's *not* in this diagram but is in some earlier designs:** AgentCore Identity (not used — Asana credentials live in SSM; GitHub uses per-owner App tokens minted server-side; Mantle uses a bearer token minted from the runtime role), AgentCore Memory (optional — honored via env var if set, not provisioned by the fleet's infra template), AgentCore Browser (not used — no agent needs a browser today). These are plausible upgrades on the roadmap. The **AgentCore Gateway** is now the fleet's shipping tool-access path — the fleet is **gateway-only**: agents route all MCP tool calls through it so the Cedar policy engine enforces per-agent tool grants + the repo allowlist at the tool-call boundary (see § 5.2 and `docs/aws-deploy.md`). There is no direct-to-vendor MCP fallback.

---

## 2. Dispatch Router Design

### 2.1 Event Flow

```
Event Source                  Normalization                  Routing
────────────                  ─────────────                  ───────

GitHub App webhook   ┐                                ┌─ Resolve agent ID
  issue_comment      │                                │  (incl. aliases from
  pr_review_comment  │                                │   the SSM registry)
  issue assigned     ├──► Dispatch Router             │
                     │    Lambda                      ├─ Authorize trigger
Asana Webhook        │    │                           │  (AVP TriggerPolicyStore,
  story added        ├──► │  Normalize to:            │   data-driven grants,
  task assigned      │    │  {                        │   fail-closed)
  custom_field set   │    │    source,                │
                     │    │    agent_id,             ├─ Check concurrency
Slack webhook        ├──► │    instruction,           │  (DynamoDB GSI query)
  app_mention        │    │    context,               │
  slash_command      ┘    │    requester              ├─ Track assignment
                          │  }                        │  (DynamoDB put)
                          │                           │
                          │                           └─ Invoke AgentCore
                          │                              Runtime (async)
```

The Asana webhook Lambda handles signature verification against `/sdlc-agents/asana-webhook-secret` (written on first handshake). The GitHub path arrives via the **GitHub App webhook Lambda** (`infra/dispatch/github_webhook.py`), which verifies the App's `X-Hub-Signature-256` HMAC, extracts the mention, fetches issue/PR context with a per-repo App token, and async-invokes the router — one App webhook covers every onboarded repo. (The earlier `agent-dispatch.yml` GitHub Actions workflow and its OIDC deploy role have been retired.) The Slack path (`infra/dispatch/slack_webhook.py`, `DeploySlack`-gated) verifies the Slack `v0` signature (±5-min replay window) and serves `/slack/events` + `/slack/commands`. **Authorization** is decided by Amazon Verified Permissions (the `TriggerPolicyStore`) against admin-authored grant *data* — the flat per-capability `authorization.users` allowlist has been removed (see §4.3.1).

### 2.2 Assignment State Machine

Today the state set is minimal: `dispatched → completed` or `dispatched → failed`. Each terminal state is written when the agent's `complete_assignment` / `fail_assignment` shared helper runs. A richer `awaiting_approval` state for approval gates is on the roadmap (see D-10 in the PRD).

### 2.3 DynamoDB Schema

**Table:** `dispatch-assignments-${STAGE}`

| Attribute | Type | Description |
|-----------|------|-------------|
| `assignment_id` (PK) | String | UUID |
| `agent_id` | String | `workitems`, `researcher`, `docwriter`, `adr` |
| `source` | String | `github`, `asana` |
| `trigger_type` | String | `comment_mention`, `assignment`, `custom_field`, `pr_comment` |
| `requester` | String | Username of the person who triggered |
| `instruction` | String | The instruction text parsed from the mention |
| `status` | String | `dispatched` → `completed` / `failed` |
| `source_context` | String (JSON) | Platform-specific context (repo, issue#, task GID, PR#) |
| `created_at` | Number | Epoch timestamp |
| `completed_at` | Number | Epoch timestamp (null until terminal) |
| `result_summary` | String | One-line summary of outcome |
| `ttl` | Number | Epoch + 30 days (auto-expire) |

**GSIs:**

- `agent_id-status-index` — active assignments per agent, used for concurrency checks.
- `source-created_at-index` — assignments by platform and time, used for reporting.

---

## 3. Agent Execution Model

### 3.1 Container Structure

Each agent is self-contained:

```
agents/<name>/
├── agent.py              # Strands agent with @app.entrypoint
├── prompts.py            # System prompt (versioned with code)
├── project_config.py     # Per-deployment env (repo, Asana GIDs) from env vars
├── tools/
│   ├── __init__.py
│   ├── <domain_tools>.py # Agent-specific @tool functions
│   ├── asana_mcp.py      # (if the agent reads Asana)
│   └── github_mcp.py     # (if the agent reads GitHub)
├── requirements.txt
├── Dockerfile
└── tests/
    └── eval_dataset.json # Golden set for future evaluations
```

Shared helpers live in `agents/shared/` (currently `assignment.py`, which provides `complete_assignment` / `fail_assignment` wrappers around the DynamoDB write).

### 3.2 Deployment Pipeline

Agents are onboarded from the dashboard Admin view (the Capabilities panel), not from a per-agent CI workflow. The base platform (foundation stack, shared build pipeline, dashboard) is deployed once with `scripts/deploy_fleet.py`, which also uploads the `agents/` tree as the build source.

```
Admin onboards agent_id in the dashboard (Capabilities panel)
      │  (admin API writes a capability row + codebuild:StartBuild)
      ▼
Shared CodeBuild: sdlc-agent-builder-${STAGE}  (AGENT_NAME override)
      ├── Ensure ECR repo exists (IMMUTABLE)
      ├── Build Docker image from agents/${AGENT_NAME}/Dockerfile (context agents/)
      └── Push image (fresh per-build tag, no :latest)
      │  (build-completion EventBridge event)
      ▼
capability-deployer Lambda
      ├── Ensure per-agent runtime IAM role (path /sdlc-agents/capabilities/,
      │     capped by a permissions boundary)
      ├── create-or-update AgentCore Runtime with the merged env
      ├── Wait for runtime READY
      ├── Mark capability active
      └── Re-render + publish the dispatch registry to SSM
```

The runtime's environment is assembled by the `capability-deployer` from the fleet base env (guardrail id/version, `GATEWAY_MCP_URL`) plus the capability row's own `env` map — this is how per-deployment config (GitHub repo, Asana GIDs) reaches the running agent without baking it into the image. A weekly EventBridge schedule (`capability-rebuilder` Lambda) re-runs the same build for every active capability to pick up security patches; a failed build or deploy never tears down a working runtime.

---

## 4. Cross-Platform Integration

### 4.1 GitHub Integration

**Inbound triggers (via the GitHub App webhook Lambda):**
- `issue_comment` containing `@<agent>` mention → `github-webhook-${STAGE}` → Dispatch Router
- `pull_request_review_comment` containing `@<agent>` mention → `github-webhook-${STAGE}` → Dispatch Router
- `issues` with assignment to a bot user (future) → same path

**Outbound actions (via the AgentCore Gateway → SCM broker):**
- Create and update issues
- Post comments (Markdown)
- Add labels
- Read files, diffs, directory listings
- Create PRs (Docwriter's doc PRs, agents don't merge)

**Authentication:** Agents hold **no** GitHub credential. GitHub tool calls are SigV4-invoked to the AgentCore Gateway; the SCM broker Lambda target (`infra/dispatch/scm_broker.py`) mints a **per-owner GitHub App installation token** per call, scoped to the called repo and the calling agent's tier ∩ the tool's least privilege. The App private key is in Secrets Manager (`sdlc-agents/github-app/private-key`); the app id/slug + webhook secret are in SSM; per-owner `installation_id`s are in the `fleet-config` table. See [`docs/specs/github-onboarding-spec.md`](specs/github-onboarding-spec.md).

### 4.2 Asana Integration

**Inbound triggers (via `asana-webhook-${STAGE}` Lambda):**
- Story added (comment with `@<agent>` mention) → Dispatch Router
- Task changed: assignee = bot user → Dispatch Router
- Task changed: "Agent" custom field set → Dispatch Router

**Outbound actions (via Asana MCP):**
- Create and update tasks
- Post comments
- Update custom fields
- Read tasks, projects, subtasks

**Authentication:** OAuth2 against Asana's MCP app, bootstrapped once via `scripts/bootstrap_asana_oauth.py`. Tokens refresh at runtime from `/sdlc-agents/asana-mcp-*` SSM paths.

### 4.3 Slack Integration

Shipped and **always deployed** (the `DeploySlack` gate was retired — the receiver is serverless/inert and fails closed, so Slack goes live only when an admin onboards a workspace). A `slack-webhook-${STAGE}` Lambda (`infra/dispatch/slack_webhook.py`) sits on the webhook API on three routes:

**Inbound triggers:**
- `/slack/events` — Events API `app_mention` ("@fleetbot @workitems break this up") → Dispatch Router
- `/slack/commands` — slash commands: `/fleet @agent …` (mention dispatch), `/sdlc-onboard-channel [agent …]` (files a channel-onboarding **request** an admin approves in the dashboard; never self-served), and `/sdlc-notify` (opens the notification-config modal)
- `/slack/interactions` — Block Kit modal submits (the `/sdlc-notify` config modal — see §4.4)

**Multi-workspace + security:** each delivery's `team_id` must resolve to an onboarded, enabled, active `slack_workspace` row. Every request is authenticated by the Slack `v0` signature over `v0:{ts}:{raw_body}` with a ±5-min replay window; deliveries are deduped on `event_id` and bot-loops are guarded. The **signing secret is app-level** (one per Slack app; the `url_verification` handshake carries no team scope) and **bot tokens are per-workspace** — both SSM SecureString under `/sdlc-agents/${STAGE}/slack/*`, fetched per-invocation, written out-of-band by `scripts/bootstrap_slack.py`.

**Outbound actions:** replies via `chat.postMessage` using the per-workspace bot token (`infra/dispatch/reply.py`); notification fan-out threads via the same helper (§4.4).

### 4.3.1 Trigger Authorization (Amazon Verified Permissions)

The Dispatch Router authorizes **every** dispatch — from all three sources — against the AVP `TriggerPolicyStore` (`infra/dispatch/trigger_authz.py`); this is the fleet's **sole** trigger-authz mechanism. The old flat per-capability `authorization.users` allowlist has been removed.

- **Data-driven:** a small, FIXED 3-policy Cedar set (permit-on-allowed, forbid-on-denied [forbid-wins], forbid-on-blocked-channel) is authored once in the foundation template. Admin-authored grants are **data** — `trigger_rule` (WHO) + `slack_workspace`/`slack_channel` (WHERE) rows in `fleet-config-${STAGE}`, read per dispatch and passed to AVP as entity attributes. Granting a user is a DynamoDB write, so the AVP policy count stays constant (avoids the policy-per-user anti-pattern).
- **Immutable principals:** `github:<login>`, `asana:<gid>`, `slack:<team>:<uid>` — never a self-editable display name.
- **Fail-closed:** an unset store, any AVP/grant-read error, or a non-`ALLOW` decision all deny. Default-deny: an agent with no permit grant is not triggerable.

See [`docs/specs/slack-connectors-spec.md`](specs/slack-connectors-spec.md) and [`docs/threat-model.md`](threat-model.md) (T-40, T-32–T-39).

### 4.4 Identity, Permission Groups & Notifications

**Cross-source identity (`infra/dispatch/identity.py`).** Every dispatch resolves its sender to one **identity** record (`identity#<uuid>` in `fleet-config`), keyed on a synthetic id with **email as the golden join attribute** (a GitHub/Asana first touch may carry no email, so email can't be the key). The resolver get-or-creates + progressively enriches the record on every touch from any source; the resolved `email` + `groups` are stamped onto trigger authz (§4.3.1), so a grant authored against an email/group applies across GitHub/Asana/Slack at once. This is the traceability spine — every assignment + authz decision maps to one person, not four disjoint handles.

**First-touch onboarding gate.** A dispatch from an unknown/pending sender creates a `pending` identity + a single `user_req#` onboarding request, and the Router replies (every time) telling the user to get onboarded — org-repo copy promises an email on completion, personal-repo copy points at the admin. An admin approves in the dashboard **Connectors → Access** panel, which flips the identity `active`, assigns permission groups, and marks its handles `verified` (admin approval is the trust event).

**Permission groups (§17).** The recommended access mechanism: membership lives on the identity record (source-agnostic), access is expressed as group-scoped `trigger_rule` rows — reusing the group axis the Cedar policy set already evaluates, no policy change.

**Notifications (`notify.py` + `slack_notify.py`, §18).** Channels self-serve tiered Slack notifications (`actionable`/`informative`/`error`) via the `/sdlc-notify` Block Kit modal (submitted on `/slack/interactions` → `notif_sub#`). The Router fans fleet lifecycle events out to subscribed channels, threaded per unit-of-work, with @mentions (resolved to the right Slack user per workspace via the identity map) only on the actionable/error tiers. Repo scope is bounded to onboarded repos.

---

## 5. Security Model

### 5.1 Identity and Authentication

| Boundary | Authentication |
|----------|----------------|
| GitHub App webhook → Lambda | `X-Hub-Signature-256` HMAC verification against the App webhook secret (SSM SecureString) |
| Asana webhook → Lambda | `X-Hook-Signature` HMAC verification against `/sdlc-agents/asana-webhook-secret` |
| Agents → Gateway → Asana MCP | Gateway SigV4 (runtime role); Asana OAuth2 held gateway-side |
| Agents → Gateway → GitHub | Gateway SigV4 (runtime role); per-owner GitHub App installation token minted by the SCM broker per call (agents hold no GitHub credential) |
| Agents → Bedrock Mantle | Short-term Bedrock bearer token minted from the runtime role (`aws-bedrock-token-generator`); guardrail applied via Mantle headers |
| Dispatch Router → AgentCore Runtime | IAM (Lambda execution role, scoped `InvokeAgentRuntime`) |
| Dashboard API → authorization | Cognito JWT at the API Gateway authorizer + Amazon Verified Permissions (Cedar `Read`/`Write`), fail-closed |

### 5.2 Cedar Policy Summary

Every agent ships with a per-agent policy file at `cedar/<agent>.cedar`, plus `cedar/shared.cedar` which forbids destructive tool calls across the entire fleet.

| Agent | Allowed | Forbidden |
|-------|---------|-----------|
| All | Read issues, PRs, tasks, files, ADRs | Merge PRs, close issues, delete tasks/branches, delete projects |
| Workitems | Create issues/tasks, post comments, add labels | All shared forbids |
| Researcher | Create tasks, post comments, update custom fields, web search | All shared forbids |
| Docwriter | Create PRs (doc files), post comments | All shared forbids; cannot modify code files |
| Adr | Post issue comments, add labels, post PR review comments | All shared forbids; cannot modify ADR files |

The per-agent `cedar/*.cedar` files document the contract; the **enforced** form lives in `infra/dashboard/fleet_policy.py`, evaluated by the **AgentCore Gateway policy engine** in the invocation path. The fleet is **gateway-only** — agents route every tool call through the Gateway, whose Cedar engine evaluates policies default-deny + forbid-wins (per-agent permits keyed on the runtime-role ARN, an unconditional destructive-tool forbid, and a repo-allowlist forbid generated from the admin config). See `docs/aws-deploy.md` § AgentCore Gateway. The engine is rolled out `LOG_ONLY` first, then flipped to `ACTIVE`; until `ACTIVE`, Cedar tool-grant *deny* decisions log rather than block, but the co-repo interceptor and the per-call scoped GitHub credential enforce regardless. The `cedar/*.cedar` files themselves remain advisory (`fleet_policy.py` is authoritative).

### 5.3 Data Security

- **Asana OAuth tokens** stored in SSM Parameter Store as SecureString (KMS-encrypted at rest); the **GitHub App private key** is in Secrets Manager. Neither lives in source or in environment variables baked into images.
- **Per-agent runtime roles** (`<agent>-agentcore-runtime`, created under IAM path `/sdlc-agents/capabilities/` and capped by the `CapabilityRuntimeBoundary` permissions boundary) grant only what the agent needs — Mantle inference + bearer-token mint, guardrail apply, gateway invoke, DynamoDB, ECR pull, logs — and no GitHub credential (the SCM broker holds the App key).
- **CloudTrail** captures every `bedrock-agentcore:InvokeAgentRuntime` and `ssm:GetParameter` call.
- **CloudWatch Logs** receive agent stdout via the OpenTelemetry distribution baked into each container.
- **No agent has access to production databases or customer PII.** Agents operate on GitHub + Asana metadata only.

---

## 6. Observability

### 6.1 What's instrumented today

- **CloudWatch Logs** for every agent runtime, Dispatch Router Lambda, and Asana webhook Lambda.
- **OpenTelemetry** via the `aws-opentelemetry-distro` Python package baked into each agent container — emits traces for Bedrock calls, MCP calls, and custom tool executions.
- **DynamoDB assignments table** is the authoritative record of what was requested and whether it succeeded. Each assignment carries the structured, traceable dimensions the dashboard reads: `trace_refs` (repo, branch, PR, issue, Jira key, Asana task — an open map any integration can extend), `participants` (requester plus assignees/commenters), and, on completion, `duration_seconds`, `token_usage`, and a derived `cost_estimate_usd`.
- **Per-assignment cost tracking.** Agents write token usage (from the model result's usage metrics) and a derived cost estimate to DynamoDB at close — no longer roadmap.
- **Fleet monitoring dashboard** (optional, `DeployDashboard=true`) — an operator-only, read-only web view over the assignments table: fleet list with filters, per-run detail, and cross-agent traceability by dimension. Served on CloudFront + S3, backed by a Cognito-authorized query API Lambda (`infra/dashboard/`). See [`dashboard/README.md`](../dashboard/README.md) and [`aws-deploy.md`](./aws-deploy.md).

### 6.2 What's not (roadmap)

- A fleet-wide **CloudWatch** dashboard aggregating native per-agent metrics (distinct from the run-tracking web dashboard above, which reads the assignments table, not CloudWatch metrics).
- Alarms on OAuth refresh failures, runtime errors, or token-budget breaches.
- AgentCore Evaluations against the per-agent `eval_dataset.json` golden sets (the datasets exist; the evaluation pipeline doesn't).
- Durable run history beyond the assignments table's 30-day TTL (the dashboard is a recent-activity view by design).

---

## 7. Cost Model

Cost is driven by three things:

1. **Bedrock model invocations** — all four agents run on Claude Sonnet 5 via the Bedrock Mantle endpoint today. A single shared fleet-wide Mantle **project** (`MANTLE_PROJECT_ID`) attributes model cost/usage; attribution is fleet-wide rather than per-repo because a dispatch may act across several repos (co-repo modes).
2. **AgentCore Runtime compute** — billed per-second during invocations.
3. **Lambda + API Gateway** for Dispatch Router and Asana webhook — pennies at typical volume.

Per-agent **daily token budgets** are carried on each agent's capability row (the `limits` field, edited in the dashboard Admin view):

| Agent | Daily token budget | Max concurrent | Timeout |
|---|---|---|---|
| workitems | 500,000 | 5 | 15 min |
| researcher | 400,000 | 3 | 20 min |
| docwriter | 300,000 | 3 | 15 min |
| adr | 300,000 | 3 | 10 min |

Actual observed cost depends on usage volume. The primary cost lever is **prompt caching** (Strands `CacheConfig`) — planned but not yet enabled.

---

## 8. What's Not Yet Built

Explicit list of things the earlier design described as load-bearing but which aren't in the shipping system. Each is plausibly a future upgrade; none are blocking adoption today.

- **AgentCore Identity.** Would replace the per-agent SSM paths with a centralized credential vault and a `@requires_access_token` decorator pattern. Upside: easier rotation, auditability. Downside: more setup friction; contributors need to understand Identity's workload-identity model.
- **AgentCore Memory (provisioned).** Agents already honor `AGENTCORE_MEMORY_ID`; what's missing is a Memory resource in the foundation stack and a story for seeding it. Upside: agents accumulate context across invocations. Downside: memory-quality governance is a non-trivial operational problem.
- **Feedback agent.** A Haiku-based agent that watches human edits to other agents' output and writes corrections to the `/feedback/` memory namespace. Useful once Memory is provisioned; depends on it.
- **UAT agent.** Playwright test generation and execution against staging. Depends on AgentCore Browser and a solid story for test-maintenance across UI changes.

*(Slack dispatch — event receiver, signature verification, and slash commands — has since shipped; see §4.3.)*

The [roadmap](roadmap.md) has the ordering.

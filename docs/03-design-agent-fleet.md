# Technical Design Document
## Autonomous PDLC Agent Fleet

**Document Version:** 2.0
**Date:** April 2026
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

```
┌─────────────────────────────────────────────────────────────────┐
│                     EXTERNAL PLATFORMS                           │
│           GitHub (Issues, PRs, Actions)  │   Asana              │
└──────────────┬──────────────────────────┴──────┬────────────────┘
               │                                 │
               ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     DISPATCH LAYER                               │
│                                                                  │
│  GitHub Actions workflow            Asana Webhook Lambda         │
│  (agent-dispatch.yml)               (asana-webhook-${STAGE})     │
│         │                                    │                   │
│         └────────────────┬───────────────────┘                   │
│                          ▼                                       │
│          Dispatch Router Lambda (dispatch-router-${STAGE})       │
│          ┌─────────────────────────────┐                         │
│          │ • Parse @mention             │                         │
│          │ • Resolve aliases            │                         │
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
│  │          │ │            │ │          │ │        │            │
│  │ Strands  │ │ Strands    │ │ Strands  │ │Strands │            │
│  │ + Opus   │ │ + Opus     │ │ + Opus   │ │+ Opus  │            │
│  │   4.7    │ │   4.7      │ │   4.7    │ │  4.7   │            │
│  └──────────┘ └────────────┘ └──────────┘ └────────┘            │
│                                                                  │
│  Each agent: Strands SDK + BedrockAgentCoreApp in a container   │
│  Model: us.anthropic.claude-opus-4-7 (all agents)               │
│  Credentials: SSM SecureString (per-tool: asana_mcp, github_mcp)│
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────┼──────────────────────────────────────┐
│                     TOOLS │                                       │
│                           ▼                                       │
│   Asana MCP                           GitHub MCP                  │
│   https://mcp.asana.com/v2/mcp        https://api.githubcopilot  │
│   (OAuth via bootstrap_asana_oauth)   .com/mcp/                   │
│                                       (PAT or GitHub App)         │
│                                                                  │
│   Tavily web search (Researcher only) Cedar policy evaluation     │
└─────────────────────────────────────────────────────────────────┘
```

**What's *not* in this diagram but is in some earlier designs:** AgentCore Identity (not used — credentials live in SSM), AgentCore Memory (optional — honored via env var if set, not provisioned by the fleet's infra template), AgentCore Browser (not used — no agent needs a browser today). These are plausible upgrades on the roadmap. **AgentCore Gateway** is now available as an opt-in (`DeployGateway`): agents route MCP tool calls through it so the Cedar policy engine can enforce the repo allowlist at the tool-call boundary (see § 5.2 and `docs/aws-deploy.md`). Absent the gateway, agents connect directly to the vendor MCP servers.

---

## 2. Dispatch Router Design

### 2.1 Event Flow

```
Event Source                  Normalization                  Routing
────────────                  ─────────────                  ───────

GitHub Actions       ┐                                ┌─ Resolve agent ID
  issue_comment      │                                │  (incl. aliases from
  pr_review_comment  ├──► Dispatch Router             │   the SSM registry)
  issue assigned     │    Lambda                      │
                     │    │                           ├─ Check authorization
Asana Webhook        ├──► │  Normalize to:            │  (authorization.users)
  story added        │    │  {                        │
  task assigned      │    │    source,                ├─ Check concurrency
  custom_field set   ┘    │    agent_id,              │  (DynamoDB GSI query)
                          │    instruction,           │
                          │    context,               ├─ Track assignment
                          │    requester              │  (DynamoDB put)
                          │  }                        │
                          │                           └─ Invoke AgentCore
                          │                              Runtime (async)
```

The Asana webhook Lambda handles signature verification against `/sdlc-agents/asana-webhook-secret` (written on first handshake). The GitHub path arrives via the `agent-dispatch.yml` workflow, which extracts the mention and invokes the router Lambda directly — no separate GitHub webhook receiver.

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

**Inbound triggers:**
- `issue_comment` containing `@<agent>` mention → `agent-dispatch.yml` → Dispatch Router
- `pull_request_review_comment` containing `@<agent>` mention → `agent-dispatch.yml` → Dispatch Router
- `issues` with assignment to a bot user (future) → same path

**Outbound actions (via GitHub MCP):**
- Create and update issues
- Post comments (Markdown)
- Add labels
- Read files, diffs, directory listings
- Create PRs (Docwriter's doc PRs, agents don't merge)

**Authentication:** Fine-grained GitHub PAT or GitHub App installation, stashed in SSM under `/sdlc-agents/github-mcp-*`. The Strands agent loads the token and passes it as a bearer header to `api.githubcopilot.com/mcp/`.

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

### 4.3 Slack Integration (roadmap)

`.dispatch/agents.yaml` advertises Slack triggers for Workitems and Docwriter, but there's no Slack event receiver in the foundation stack and the Dispatch Router has no Slack signature verifier. Adding Slack requires a `slack-webhook-${STAGE}` Lambda, a Slack app manifest, and the signing-secret path in SSM. Flagged as a gap in `skills/pdlc-agents-register-triggers`.

---

## 5. Security Model

### 5.1 Identity and Authentication

| Boundary | Authentication |
|----------|----------------|
| GitHub Actions → AWS | GitHub OIDC (no stored credentials) |
| Agents → Asana MCP | OAuth2 refresh-token flow; tokens in SSM SecureString |
| Agents → GitHub MCP | PAT or GitHub App installation token; credentials in SSM SecureString |
| Agents → Bedrock models | IAM execution role attached to the AgentCore Runtime |
| Dispatch Router → AgentCore Runtime | IAM (Lambda execution role) |
| Asana webhook → Lambda | `X-Hook-Signature` HMAC verification against `/sdlc-agents/asana-webhook-secret` |

### 5.2 Cedar Policy Summary

Every agent ships with a per-agent policy file at `cedar/<agent>.cedar`, plus `cedar/shared.cedar` which forbids destructive tool calls across the entire fleet.

| Agent | Allowed | Forbidden |
|-------|---------|-----------|
| All | Read issues, PRs, tasks, files, ADRs | Merge PRs, close issues, delete tasks/branches, delete projects |
| Workitems | Create issues/tasks, post comments, add labels | All shared forbids |
| Researcher | Create tasks, post comments, update custom fields, web search | All shared forbids |
| Docwriter | Create PRs (doc files), post comments | All shared forbids; cannot modify code files |
| Adr | Post issue comments, add labels, post PR review comments | All shared forbids; cannot modify ADR files |

The per-agent `cedar/*.cedar` files document the contract. Hard enforcement is available via the **AgentCore Gateway policy engine** (opt-in, `DeployGateway`): agents route tool calls through the Gateway, whose Cedar engine evaluates policies in the invocation path (default-deny + forbid-wins). The fleet repo-allowlist policy is generated from the admin config and enforced there — see `docs/aws-deploy.md` § AgentCore Gateway. Rolled out `LOG_ONLY` first; the per-agent tool-scope rules above are folded into the engine (rewritten to gateway `<Target>___<tool>` actions) as part of that migration. Until the gateway is deployed and set to `ACTIVE`, the `cedar/*.cedar` files remain advisory.

### 5.3 Data Security

- **OAuth tokens and PATs** stored in SSM Parameter Store as SecureString (KMS-encrypted at rest), never in source or in environment variables baked into images.
- **Per-agent runtime roles** (`<agent>-agentcore-runtime`) with narrow `ssm:GetParameter` access — Workitems can read Asana + GitHub MCP creds, Adr can read only GitHub MCP creds, etc.
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

1. **Bedrock model invocations** — all four agents run on Claude Opus 4.7 today.
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
- **AgentCore Gateway.** Would replace direct MCP connections with a single managed MCP endpoint. Upside: one place to authorize, rate-limit, and observe tool calls. Downside: one more hop to debug and a Gateway cold-start path to manage.
- **AgentCore Memory (provisioned).** Agents already honor `AGENTCORE_MEMORY_ID`; what's missing is a Memory resource in the foundation stack and a story for seeding it. Upside: agents accumulate context across invocations. Downside: memory-quality governance is a non-trivial operational problem.
- **Feedback agent.** A Haiku-based agent that watches human edits to other agents' output and writes corrections to the `/feedback/` memory namespace. Useful once Memory is provisioned; depends on it.
- **UAT agent.** Playwright test generation and execution against staging. Depends on AgentCore Browser and a solid story for test-maintenance across UI changes.
- **Slack dispatch.** Event receiver, signature verification, and slash commands.

The [roadmap](roadmap.md) has the ordering.

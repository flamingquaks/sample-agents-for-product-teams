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
│    GitHub (Issues, PRs, Actions)  │  Asana  │  Slack            │
└──────────────┬──────────────────────────┴──────┬────────────────┘
               │                                 │
               ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     DISPATCH LAYER                               │
│                                                                  │
│  GitHub Actions workflow  Asana Webhook Lambda  Slack Webhook    │
│  (agent-dispatch.yml)     (asana-webhook-${STAGE}) (slack-webhook-${STAGE}) │
│         │                        │                   │           │
│         └────────────────┬───────┴───────────────────┘           │
│                          ▼                                       │
│          Dispatch Router Lambda (dispatch-router-${STAGE})       │
│          ┌─────────────────────────────┐                         │
│          │ • Parse @mention             │                         │
│          │ • Resolve aliases            │                         │
│          │ • Check authorization        │                         │
│          │ • Track in DynamoDB          │                         │
│          │ • Invoke AgentCore Runtime   │                         │
│          └─────────────────────────────┘                         │
│          Config: SSM (synced from .dispatch/agents.yaml)         │
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

**What's *not* in this diagram but is in some earlier designs:** AgentCore Identity (not used — credentials live in SSM), AgentCore Gateway (not used — agents connect directly to vendor MCP servers), AgentCore Memory (optional — honored via env var if set, not provisioned by the fleet's infra template), AgentCore Browser (not used — no agent needs a browser today). These are all plausible upgrades on the roadmap.

---

## 2. Dispatch Router Design

### 2.1 Event Flow

```
Event Source                  Normalization                  Routing
────────────                  ─────────────                  ───────

GitHub Actions       ┐                                ┌─ Resolve agent ID
  issue_comment      │                                │  (incl. aliases from
  pr_review_comment  ├──► Dispatch Router             │   agents.yaml)
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

The Asana webhook Lambda handles signature verification against `/sdlc-agents/asana-webhook-secret` (written on first handshake). The Slack webhook Lambda handles HMAC-SHA256 verification against `/sdlc-agents/slack-signing-secret`, supports both `app_mention` events and slash commands, and pre-resolves the agent ID before passing it to the router. The GitHub path arrives via the `agent-dispatch.yml` workflow, which extracts the mention and invokes the router Lambda directly — no separate GitHub webhook receiver.

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

```
git push (change under agents/<name>/** or agents/shared/**)
      │
      ▼
GitHub Actions: deploy-<name>.yml
      │ (passes required env vars from repo Actions Variables)
      ▼
Reusable workflow: deploy-agent.yml
      ├── Validate env_vars (fail-fast if required value empty)
      ├── Configure AWS credentials via OIDC
      ├── Ensure ECR repo exists
      ├── Build Docker image, tag with commit SHA + latest, push
      ├── Amazon Inspector SBOM scan
      ├── create-or-update AgentCore Runtime (create on first run)
      ├── Wait for runtime READY
      ├── Sync dispatch registry to SSM (resolves runtime ARN placeholders)
      └── Smoke test (invoke runtime with {"prompt": "health check"})
```

The shared workflow also supports the `env_vars` input, which is passed through to `create-agent-runtime` / `update-agent-runtime` as the container's environment — this is how per-deployment config (GitHub repo, Asana GIDs) reaches the running agent without baking it into the image.

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

### 4.3 Slack Integration

All four agents accept `@mention` and slash-command triggers from Slack via the `slack-webhook-${STAGE}` Lambda backed by `POST /slack/events` and `POST /slack/commands` on the shared WebhookApi.

**Inbound triggers (via `slack-webhook-${STAGE}` Lambda):**
- `app_mention` — user types `@SDLC Agents @workitems <instruction>` in any channel the bot is in → Dispatch Router
- Slash command — user types `/workitems <instruction>`, `/docwriter <instruction>`, etc. → ephemeral ack → async Dispatch Router invoke

**Outbound actions (via `agents/shared/slack_post.py`):**
- `slack_post_message` — post to a channel at the top level or in a thread
- `slack_post_thread` — convenience wrapper to reply in a specific thread

**Authentication:** Bot token (`xoxb-…`) in SSM at `/sdlc-agents/slack-bot-token`; signing secret at `/sdlc-agents/slack-signing-secret`. Both stored by `scripts/bootstrap_slack_app.py`.

**Signature verification:** HMAC-SHA256 of `v0:{timestamp}:{body}` against the signing secret, with a 5-minute replay window. The Lambda hard-fails on a missing or empty signing secret (mirrors Asana's T-8/T-9 posture).

**Single-bot model:** one Slack app covers the entire fleet. Agent resolution mirrors the Asana/GitHub pattern: first `@agent` mention in the stripped message text, with the same alias map from `.dispatch/agents.yaml`.

### 4.4 Discord Integration (slash commands)

Discord slash commands are supported in v1. Users invoke agents via `/workitems <instruction>`, `/docwriter <instruction>`, etc. in any Discord channel where the bot has been installed.

**Inbound (via `discord-webhook-${STAGE}` Lambda):**
- Slash commands are delivered to `POST /discord/interactions` via Discord's Interactions Endpoint.
- Signature: Ed25519 — Discord signs `(X-Signature-Timestamp + raw body)` with the application private key. The receiver verifies against the public key stored in SSM as a plain String (public by nature, but kept in SSM for consistent config).
- PING (type 1): echoed immediately within 3 seconds per Discord's protocol requirement.
- APPLICATION_COMMAND (type 2): acknowledged with a deferred response (type 5, "Bot is thinking..."), then the Dispatch Router is invoked asynchronously. The agent posts its reply via `POST /webhooks/{application_id}/{interaction_token}` using `discord_post_followup`.
- Agent is pre-resolved from the slash command name; passed to the router via the `agent_id` payload field (same pattern as the Asana receiver).

**Outbound (via `agents/shared/discord_post.py` @tool functions):**
- `discord_post_followup(application_id, interaction_token, content)` — interaction-token reply, valid 15 minutes, no auth header needed.
- `discord_post_message(channel_id, content)` — channel post via bot token from SSM.
- HTTP 429 handled with one bounded Retry-After retry (Discord rate limits are tighter than Slack/Asana).

**Authentication:**
- Bot token stored in SSM SecureString at `/sdlc-agents/discord-bot-token`.
- Application public key (not a secret) stored as String at `/sdlc-agents/discord-public-key`.
- Bootstrapped by `scripts/bootstrap_discord_app.py`.

**`@mention` events (Gateway listener — roadmap):**
Discord does not deliver `@mention` events over HTTP. To receive them, a long-lived WebSocket listener must subscribe to Discord's Gateway with `MESSAGE_CREATE` intents. A Lambda is the wrong shape for this (it cannot hold a persistent WebSocket). The intended architecture is a small Fargate task that subscribes to the Gateway and POSTs normalized events to the same `/discord/interactions` endpoint (or directly to the Dispatch Router). This is out of scope for v1; slash commands cover the primary interaction pattern.

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

Cedar evaluation today is advisory — the policies document the contract. Hard enforcement via a Cedar evaluator in the invocation path is a roadmap item.

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
- **DynamoDB assignments table** is the authoritative record of what was requested and whether it succeeded.

### 6.2 What's not (roadmap)

- A fleet-wide CloudWatch dashboard aggregating per-agent metrics.
- Alarms on OAuth refresh failures, runtime errors, or token-budget breaches.
- AgentCore Evaluations against the per-agent `eval_dataset.json` golden sets (the datasets exist; the evaluation pipeline doesn't).
- Per-assignment cost tracking. Token usage is available from Bedrock's response metadata but not yet surfaced to DynamoDB.

---

## 7. Cost Model

Cost is driven by three things:

1. **Bedrock model invocations** — all four agents run on Claude Opus 4.7 today.
2. **AgentCore Runtime compute** — billed per-second during invocations.
3. **Lambda + API Gateway** for Dispatch Router and Asana webhook — pennies at typical volume.

Per-agent **daily token budgets** are defined in `.dispatch/agents.yaml`:

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
The [roadmap](roadmap.md) has the ordering.

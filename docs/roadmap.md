# PDLC Agent Fleet Roadmap

A short, honest roadmap. What shipped, what's next, and what's deferred. The aspirational "v2 Features" section from an earlier draft of this doc has been moved to a separate [ideas](#ideas-not-on-the-roadmap) section at the bottom — those aren't commitments, just a list of things worth considering when we have bandwidth.

## Vision

An autonomous agent fleet that handles the operational burden of software development — planning, documentation, business analysis, architectural alignment — so small teams can focus on building product.

---

## Shipped

| Agent | Role | Status |
|---|---|---|
| **Workitems** | PO/PM: work decomposition (approval pattern), status reports, risk detection, Asana ↔ GitHub sync | Live |
| **Researcher** | Business analyst: research synthesis, competitive scans, story drafting, spec review | Live |
| **Docwriter** | Technical writer: API docs, release notes, doc PRs, freshness checks | Live |
| **Adr** | ADR linker: tags issues with governing ADRs, reviews PRs against them | Live |

Supporting infrastructure shipped:
- **Dispatch Router** Lambda routes `@mention` events from GitHub, Asana, and Slack to the right runtime.
- **Asana webhook receiver** Lambda handles Asana event subscription, signature verification, and normalization.
- **GitHub App webhook receiver** Lambda handles the GitHub mention path (HMAC-verified). *(This replaced the earlier `agent-dispatch.yml` GitHub Actions workflow + OIDC deploy role, both retired.)*
- **Slack connector** (`DeploySlack`-gated) — `slack-webhook` Lambda serving `/slack/events` + `/slack/commands` (Slack `v0` signature + ±5-min replay window, `event_id` dedup, bot-loop guard). Multi-workspace; users request channel access via `/sdlc-onboard-channel` and admins approve in the dashboard Connectors → Slack panel.
- **Trigger authorization via Amazon Verified Permissions** — the `TriggerPolicyStore` (a fixed Cedar policy set over admin-authored grant *data*: `trigger_rule`/`slack_workspace`/`slack_channel` rows) is the sole trigger-authz mechanism; the old per-capability `authorization.users` allowlist has been removed. Granting a user is a DynamoDB write, not a new policy. Fail-closed.
- **AgentCore Gateway (gateway-only tool access)** — all agent MCP tool calls route through one managed Gateway with a Cedar policy engine + SCM co-repo interceptor; GitHub is fronted by the `scm-broker` Lambda minting per-owner App tokens, Asana by a direct MCP target. (Replaced the per-agent direct-to-vendor connections.)
- **Per-agent Cedar policies** — `cedar/<agent>.cedar` is the advisory source; the enforced form lives in `infra/dashboard/fleet_policy.py`, evaluated by the Gateway policy engine.
- **UI-driven agent onboarding** — the dashboard Admin view onboards an agent by writing a capability row; a shared `sdlc-agent-builder-<stage>` CodeBuild project builds its container and the `capability-deployer` Lambda stands up its runtime and republishes the registry. A weekly `capability-rebuilder` schedule rebuilds every active agent for security patches. Base platform deployed with `scripts/deploy_fleet.py`.
- **Skills** under `skills/` drive guided install into a new repo/account.
- **Fleet monitoring + admin dashboard** (`dashboard/`) — React + Vite SPA showing run history, traces, fleet status, and the Admin view for onboarding agents/repos plus the **Connectors** section (Slack/Asana/GitHub + trigger rules + channel requests). Backed by query + admin API Lambdas (`infra/dashboard/`), hosted on S3 + CloudFront, published by `scripts/deploy_fleet.py`.

All four agents run Claude Sonnet 5 via Bedrock Mantle.

---

## Near-term (next 1–2 quarters, order is priority)

1. **Provision AgentCore Memory in the foundation template.** Agents already honor `AGENTCORE_MEMORY_ID`; what's missing is a Memory resource in `infra/foundation/template.yaml` and a documented seeding path per agent.
2. **Flip Gateway Cedar enforcement to `ACTIVE`.** The Gateway policy engine ships in `LOG_ONLY` first (`GatewayPolicyEnforcement`); the tool-grant deny decisions log rather than block until an operator flips it to `ACTIVE` after watching CloudWatch. (The co-repo interceptor + per-call scoped credential enforce regardless.)
3. **Dispatch circuit breaker.** Thread `parent_assignment_id` through dispatch and enforce chain-depth, per-agent rolling-rate, and daily-token-budget limits in the Router with CloudWatch alarms on trip (threat-model T-21/T-22/T-23).
4. **AgentCore Evaluations.** Each agent ships with `tests/eval_dataset.json`. The evaluation pipeline that scores runs against those datasets isn't wired up.
5. **Per-assignment cost tracking.** Token usage is available in Bedrock response metadata; surface it to the DynamoDB assignments table so we can report cost per run per agent.

---

## Medium-term (the interesting work, no commitment)

### Jira + GitLab support
Atlassian has an official remote MCP server (Jira, Confluence, Compass under one OAuth). The agent-side changes are small (point at a different MCP URL, tune prompts for Jira terminology). The dispatch side needs a Jira webhook receiver.

GitLab is harder — no production-grade official remote MCP. Community options exist (`zereight/gitlab-mcp`). Each shipping agent's system prompt would need merge-request vs. pull-request terminology adjustments.

### UAT agent
Generate Playwright tests from user stories and run them against staging. Requires a real solution for test-maintenance across UI changes, not just test-generation. Depends on AgentCore Browser (or a browser-in-Lambda fallback).

### Feedback agent
A Haiku-based agent that watches human edits to other agents' output and writes corrections to the fleet's memory. Depends on Memory being provisioned and populated (item 1 above).

### Cedar enforcement in Dispatch
Beyond tool-call-time enforcement, evaluate the full policy graph at the assignment level so the router can reject attempts before invoking the runtime.

---

## Ideas — not on the roadmap

Collected from earlier design sessions. Worth considering, not committed to.

- **Merge agent** — PR readiness checklist, release assembly, GTM handoff. Design doc deleted; spec was aspirational.
- **Triage agent** — Support case triage against known issues; escalate novel ones.
- **Diagnostics agent** — Incident timeline reconstruction from Datadog + PagerDuty.
- **Monitor agent** — Scheduled observability sweeps; file new issues for novel patterns.
- **Securityreviewer agent** — Threat-modeling partner on design docs; security review on PRs.
- **Bugreproducer agent** — Reproduce filed bugs with a failing test.
- **Gtm agent** — Changelogs and announcement drafts from releases.
- **Figma integration** — Pull design tokens into Docwriter, wire Figma webhooks to Dispatch.
- **AgentCore Identity migration** — Replace the SSM credential paths with a centralized Identity vault and `@requires_access_token` pattern.
- **Trello, Aha!, Linear PM support** — Workitems with different backends.

---

## Document Index

Current docs in this repo:

| Document | Purpose |
|----------|---------|
| [`01-prfaq-agent-fleet.md`](01-prfaq-agent-fleet.md) | Press release + FAQ framing the problem and solution |
| [`02-prd-agent-fleet.md`](02-prd-agent-fleet.md) | Requirements (Shipped / Roadmap per item) |
| [`03-design-agent-fleet.md`](03-design-agent-fleet.md) | System architecture as shipped |
| [`aws-deploy.md`](aws-deploy.md) | What the project provisions in AWS + deterministic-deploy requirements |
| [`agent-fleet-implementation-plan.md`](agent-fleet-implementation-plan.md) | Status doc (shipped vs. deferred) |
| [`agents/*.md`](agents/) | Per-agent design docs for the four shipping agents |
| [`specs/*.md`](specs/) | Detailed specs for each shipping agent, the Dispatch routing layer, and the Slack connectors + trigger-authz spec |
| [`threat-model.md`](threat-model.md) | Living threat model (STRIDE / OWASP LLM), updated as the fleet ships |
| [`ai-sdlc-toolchain-map.md`](ai-sdlc-toolchain-map.md) | Role-by-role toolchain thinking (context, not spec) |
| [`bot-patterns-recommendations.md`](bot-patterns-recommendations.md) | Bot-pattern survey (context, not spec) |

# AWS Deploy Surface

What this project provisions in AWS, and what inputs you need to make a deploy deterministic from scratch.

## 0. Architecture

The rendered architecture diagram is [`docs/assets/architecture.mmd`](assets/architecture.mmd)
(Mermaid — open in any Mermaid renderer). It shows the four planes the deploy
surface below provisions:

**Trigger plane.** Users `@mention` an agent in GitHub, Asana, or Slack. Three
public, signature-verified **webhook** Lambdas receive those events: the **GitHub
App webhook** (`infra/dispatch/github_webhook.py`, verifying `X-Hub-Signature-256`),
the **Asana webhook** (`infra/dispatch/asana_webhook.py`), and the **Slack webhook**
(`infra/dispatch/slack_webhook.py`, `DeploySlack`-gated — verifies the Slack `v0`
signature with a ±5-min replay window, serves `/slack/events` + `/slack/commands`).
Each validates the signature, resolves the mentioned agent against the live
registry (shared `infra/dispatch/mentions.py`), and async-invokes the Dispatch
Router. There is **no GitHub Actions / OIDC dispatch path** — one App webhook serves
every onboarded repo, so no per-repo workflow or repo-side AWS credential is needed.

**Dispatch + agent plane.** The **Dispatch Router** Lambda runs an edge
prompt-injection guardrail (`bedrock-runtime` `apply_guardrail`), **authorizes the
trigger via Amazon Verified Permissions** (the `TriggerPolicyStore` — a fixed
Cedar policy set over admin-authored grant *data*; `infra/dispatch/trigger_authz.py`,
fail-closed; this is the sole trigger-authz mechanism — the old per-capability
`authorization.users` allowlist is removed), reads its registry from an SSM parameter
(rendered from the fleet-config capability rows), records the assignment in
DynamoDB, and calls `InvokeAgentRuntime` on the target agent's **AgentCore
Runtime** container. Agents run the Strands SDK and call models on the
**Bedrock Mantle** endpoint (Claude Sonnet 5; short-term bearer token from the
runtime role; the prompt-injection guardrail is applied via Mantle headers and
is fail-closed; the fleet's single shared Mantle **project** — injected as the
`MANTLE_PROJECT_ID` runtime env — is set as the `OpenAI-Project` header for cost
attribution). All MCP **tool** calls are **gateway-only**: agents SigV4-invoke
the **AgentCore Gateway**, whose Cedar policy engine + REQUEST interceptor enforce
per-agent tool grants and per-origin co-repo grouping before a call reaches the
GitHub SCM broker (which mints a per-owner GitHub App token) or the Asana MCP
target.

**Control plane (dashboard).** The optional operator dashboard is a React/Vite
SPA on S3 + CloudFront, behind an API Gateway with a Cognito authorizer. Every
API request is authorized by **Amazon Verified Permissions** (Cedar `Read` for
operators, `Write` for admins — `infra/dashboard/auth.py`, fail-closed). A
read-only **query** Lambda serves run history from DynamoDB; an **admin** Lambda
onboards agents and repos and manages **Connectors** (Slack workspaces/channels,
trigger rules, and channel-onboarding requests) into the `fleet-config` DynamoDB
table (and holds only `codebuild:StartBuild` — no privileged IAM). The admin's
trigger-rule writes are the *data* the Router's AVP trigger authz reads; granting
a user is a DynamoDB write, not a new Cedar policy.

**Onboarding pipeline.** Onboarding an agent writes a capability row and starts
the shared **`sdlc-agent-builder-<stage>` CodeBuild** project (parameterized by
`AGENT_NAME`), which builds `agents/<name>` and pushes to ECR. A build-completion
**EventBridge** event invokes the **capability-deployer** Lambda — the only
component holding `iam:CreateRole`/`PassRole` + `create/update-agent-runtime` —
which creates the per-agent runtime IAM role (IAM path `/sdlc-agents/capabilities/*`,
capped by the `CapabilityRuntimeBoundary` permissions boundary), deploys the
AgentCore runtime, waits READY, marks the capability active, and republishes the
registry. A weekly EventBridge schedule rebuilds every active agent for security
patches. See [`docs/threat-model.md`](threat-model.md) for the security analysis
of each plane.

## 1. What gets deployed

Everything lives in a single AWS account + region. There are three layers of resources:

### 1.1 Foundation stack (`infra/foundation/template.yaml`)

Deployed once per stage with `sam deploy`. Creates:

| Resource | Logical name | Purpose |
|---|---|---|
| DynamoDB table | `dispatch-assignments-${Stage}` | Assignment tracking (PK `assignment_id`; GSIs on `agent_id+status`, `source+created_at`); 30-day TTL |
| DynamoDB table | `fleet-config-${Stage}` | Runtime config + authz data (PK `pk`; every row tagged with a `kind`). `kind-index` GSI (partition `kind`, sort `pk`) so each "all rows of one kind" read — repos, identities, trigger rules, Slack workspaces/channels, notif subs — is a bounded Query, not a full-table Scan. PITR on |
| S3 bucket | `sdlc-agent-artifacts-${AWS::AccountId}-${Stage}` | Agent output artifacts (screenshots, test results); SSE-AES256; lifecycle rules on `screenshots/` (90d) and `test-results/` (180d) |
| Lambda | `dispatch-router-${Stage}` | Parses `@mentions`, authorizes the trigger via AVP, invokes the right AgentCore Runtime, writes assignment to DynamoDB |
| Lambda | `github-webhook-${Stage}` | Verifies the GitHub App `X-Hub-Signature-256`, resolves the mention, invokes Dispatch Router async, and fans SCM events (PR opened/merged/review-requested, issue opened) out to subscribed Slack channels via `notify.py` (reads the per-workspace Slack bot token from `/sdlc-agents/${Stage}/slack/*`; threads posts via the assignments table) |
| Lambda | `asana-webhook-${Stage}` | Verifies Asana webhook signatures, normalizes events, invokes Dispatch Router async |
| Lambda | `slack-webhook-${Stage}` | Always deployed. Verifies the Slack `v0` signature (±5-min replay window), serves `/slack/events`, `/slack/commands`, and `/slack/interactions` (the `/sdlc-notify` modal), dedups `event_id`, invokes Dispatch Router async. Inert until an admin onboards a workspace |
| API Gateway | `WebhookApi` | Fronts the webhook Lambdas at `/github/webhook`, `/asana/webhook`, and (when Slack is enabled) `/slack/events` + `/slack/commands` |
| AVP policy store | `TriggerPolicyStore` (`SdlcTrigger` schema) | **Always-on.** The fleet's trigger-authorization store — a fixed 3-policy Cedar set evaluating admin-authored grant *data* (`trigger_rule`/`slack_workspace`/`slack_channel` rows in `fleet-config-${Stage}`). The Dispatch Router reads it on every dispatch (fail-closed) |
| SSM parameter | `/sdlc-agents/${Stage}/registry` | Dispatch Router registry, rendered from the active capability rows in `fleet-config-${Stage}` (re-written on every capability change by the admin API / capability deployer) |
| CloudWatch alarms | Dispatch + webhook alarms | Dispatch error rate, webhook error rate (per receiver, incl. Slack when enabled), dispatch p99 duration |

**Outputs:** `AssignmentsTableName`, `ArtifactsBucketName`, `DispatchRouterArn`, `WebhookEndpoint` (Asana webhook URL), `GitHubWebhookEndpoint`, `TriggerPolicyStoreId`, `WebhookApiId`, `SlackEventsEndpoint`, `SlackCommandsEndpoint`, `SlackInteractionsEndpoint` (always emitted — set them on the Slack app when you onboard a workspace).

**Optional — fleet monitoring dashboard (`DeployDashboard=true`).** Off by
default; set the SAM parameter `DeployDashboard=true` to provision an
operator-only, read-only web view of agent runs. When enabled the foundation
stack additionally creates:

| Resource | Logical name | Purpose |
|---|---|---|
| Cognito user pool + group + client + domain | `sdlc-agents-dashboard-${Stage}` (+ `operators` group) | Operator login (Hosted UI, PKCE); the `operators` group gates API access |
| Lambda + API Gateway | `dashboard-query-${Stage}` / `DashboardApi` | Read-only query API (`/runs`, `/runs/{id}`, `/trace`, `/stats`), Cognito-authorized |
| S3 bucket + CloudFront (OAC) | `sdlc-agent-dashboard-${AWS::AccountId}-${Stage}` / `DashboardDistribution` | Hosts the SPA (private bucket, served only via CloudFront) |

Additional outputs (present only when enabled): `DashboardUrl` (the operator
entry point), `DashboardApiEndpoint`, `DashboardUserPoolId`,
`DashboardUserPoolClientId`, `DashboardLoginDomain`, `DashboardSiteBucketName`,
`DashboardDistributionId`. The SPA lives in `dashboard/` and is published by
`scripts/deploy_fleet.py` (build → write `config.json` from these outputs → S3
sync → CloudFront invalidation). Operators are created by an admin (no self
sign-up) and must be added to the `operators` group; admins (who onboard agents
and repos in the Admin view) go in the `admins` group. See `dashboard/README.md`.

**Agent authoring (with `DeployDashboard=true`).** The dashboard also carries
the agent-authoring surface (`docs/specs/agent-authoring-spec.md`): the stack
additionally creates

| Resource | Logical name | Purpose |
|---|---|---|
| S3 bucket | `sdlc-agent-skills-${AWS::AccountId}-${Stage}` | Uploaded `SKILL.md` packages, expanded as `skills/<scope>/<name>/` trees (KMS SSE, versioned, private; `_staging/` uploads expire after 1 day). The generic base agent pulls its referenced packages from here on startup, verifying each normalized-tree sha256 |
| Lambda | `capability-skill-unpacker-${Stage}` | **Isolated** `.zip` skill expansion (spec §6.3) — the only privilege is object read/write on the skills bucket; the admin API stages the raw zip and invokes this synchronously, never unpacking in-process |
| CodeBuild project | `sdlc-agent-builder-${Stage}` | The shared build. For a **custom** (config-driven) agent — no `agents/<id>/Dockerfile` — it builds the generic base image (`agents/_base/`), materializing per-agent pip deps from the capability row via `gen_requirements.py` (re-validated before pip; spec §7.1) |
| IAM managed policy | `sdlc-capability-runtime-boundary-${Stage}` | Permissions boundary capping every per-agent runtime role; its S3 read is scoped to the skills bucket only |

### 1.2 Per-agent runtime (created by UI onboarding, not SAM, not CI)

Agents are onboarded from the dashboard Admin view (the **Capabilities** panel). Onboarding writes a capability row to `fleet-config-${Stage}` and drives the rest of the lifecycle through the shared build pipeline and the capability deployer — no per-agent script, workflow, or manual command:

| Resource | Logical name pattern | Created by |
|---|---|---|
| ECR repository | `sdlc-agents/<agent>` | The shared `sdlc-agent-builder-${Stage}` CodeBuild buildspec (idempotent `ecr describe-repositories` or `create-repository`) |
| Container image | `sdlc-agents/<agent>:<build-tag>` in ECR (repos are `IMMUTABLE` — one tag per build, no `:latest`) | `sdlc-agent-builder-${Stage}` CodeBuild (parameterized by `AGENT_NAME`, building `agents/<agent>` from the uploaded build source) |
| IAM role | `<agent>-agentcore-runtime` (under IAM path `/sdlc-agents/capabilities/`) | `capability-deployer-${Stage}` Lambda, created with a mandatory permissions boundary |
| AgentCore Runtime | `<agent>` | `capability-deployer-${Stage}` Lambda, on the build-completion EventBridge event |

The flow: the admin API starts one shared build (it holds only `codebuild:StartBuild`); on build completion an EventBridge rule invokes the `capability-deployer` Lambda (the only component holding `iam:CreateRole`/`PassRole` + `create/update-agent-runtime`), which ensures the runtime role, deploys the runtime, waits for READY, marks the capability active, and republishes the registry. A weekly EventBridge schedule invokes the `capability-rebuilder` Lambda, which re-runs the same build for every active capability (security patching); a failed build or deploy never tears down a working runtime.

Four **built-in** agents ship in `agents/` today: `workitems`, `researcher`, `docwriter`, `adr` — seeded as fixed, enable/disable-only capability rows by `scripts/deploy_fleet.py`. Beyond those, admins can **author custom agents from the dashboard** (spec `docs/specs/agent-authoring-spec.md`): a custom agent is a capability row (system prompt + pip requirements + per-tool grants + skills) built on the **generic base image** (`agents/_base/`) — no code checkin, no per-agent Dockerfile. The buildspec discriminates on the presence of `agents/<id>/Dockerfile`: built-ins build their own image; custom agents build the base image with `requirements-extra.txt` generated (and §7.1-re-validated) from the capability row. Deleting a custom agent destroys its runtime, role, image, and capability-scoped skills; built-ins are undeletable (409 — disable instead).

### 1.3 GitHub triggers — no CI, no OIDC deploy role

The `@mention` trigger path is a **GitHub App webhook**, not GitHub Actions. The
old `agent-dispatch.yml` (and `claude-code.yml`) workflows, the GitHub OIDC
provider, and the CI deploy role that those workflows depended on have all been
**retired** — there is no CI deploy or CI dispatch path in the fleet. This repo's
`.github/workflows/` now holds lint and security-scan workflows only; none of
them assume an AWS role.

- **Trigger auth is server-side.** The fleet's GitHub App is registered once (via
  the dashboard manifest flow), and its webhook deliveries are HMAC-verified by
  the `github-webhook-${Stage}` Lambda. Onboarded repos need no per-repo workflow
  and no repo-side AWS credential — one App webhook serves every repo the App is
  installed on.
- **Build/deploy is server-side.** Agent images are built by the shared CodeBuild
  project and deployed by the `capability-deployer` Lambda (§1.2); the base
  platform is deployed by `scripts/deploy_fleet.py` / `scripts/bootstrap.py`
  running with a privileged human's own credentials. Nothing in CI needs
  `bedrock-agentcore:InvokeAgentRuntime`, ECR push, `iam:PassRole`, or S3/CloudFront
  publish.

### 1.4 Optional: Claude Code on Bedrock (one-time, per repo)

**Separate optional feature, not part of the fleet's trigger/deploy path.** If you
use the `sdlc-agents-setup-claude-code` skill, it wires the Claude Code assistant
(a GitHub Action) into a *target* repo so `@claude` can respond on issues/PRs. That
feature is the one place that still uses GitHub Actions + OIDC — Claude Code runs
in CI and calls Bedrock `InvokeModel` directly, independent of the fleet's Mantle
model path. The skill creates an IAM role with `bedrock:InvokeModel` on its chosen
model's inference profile, a GitHub OIDC provider/trust for the target repo, and
the `CLAUDE_CODE_ROLE_ARN` secret + `CLAUDE_CODE_AWS_REGION` variable on that repo.
Deploy it or not — the fleet works without it.

## 2. Inputs required for a deterministic deploy

### 2.1 AWS account + region

- **`AWS_ACCOUNT_ID`** — 12-digit account ID. Set in the deploying shell's environment (used by `scripts/deploy_fleet.py` / `scripts/bootstrap.py`).
- **`AWS_REGION`** — deployment region (same shell env). Must have Bedrock Mantle access + the Claude Sonnet 5 model available in-region (see §2.2).

### 2.2 Bedrock (Mantle) model access

Agents call models on the OpenAI-compatible **Bedrock Mantle** endpoint (default `anthropic.claude-sonnet-5`); enable Bedrock model access for that model in the Bedrock console → Model access, in the same region as `AWS_REGION`. The Router's edge guardrail and the ADR agent's Titan embeddings use classic `bedrock-runtime` in the same region. Without model access, invocations return `AccessDeniedException`.

**One-time: activate the Mantle project CFN resource type.** The foundation stack provisions the fleet's shared cost-attribution project as `AWS::BedrockMantle::Project` (`DeployMantleProject=true`, the default). That resource type is registered but must be activated per account+region before the first deploy, or the stack fails with a "Resource type not found" error:

```
aws cloudformation activate-type --type RESOURCE \
  --type-name AWS::BedrockMantle::Project --region <AWS_REGION>
```

Run it once per account+region. If you'd rather not manage the project in CloudFormation, set `DeployMantleProject=false` and pass a pre-existing id via `MantleProjectId` (or leave both unset to run on the account's default Mantle project).

### 2.3 SAM parameters (for the foundation stack)

Passed to `sam deploy --parameter-overrides`:

- **`Stage`** — `dev` / `staging` / `prod`. Embedded in every resource name.
- **`WorkitemsBotGID`** — Asana user GID that tasks are assigned to to trigger Workitems.
- **`AgentFieldGID`** — Asana custom field GID for the "Agent" dropdown (optional — empty string is fine if you're not using custom-field triggers).
- **`DeployDashboard`** — `true`/`false` (default `false`). Provisions the Cognito user pool, the operator/admin read+write APIs, and the CloudFront SPA.
- **`DeployGateway`** — `true`/`false` (default `false`). Provisions the AgentCore Gateway + Cedar policy engine (the deterministic tool-call boundary). Requires `DeployDashboard=true` (the admin API owns the policy sync).
- *(retired)* **`DeploySlack`** — removed (spec §19). The Slack receiver is now **always deployed**: it's serverless + inert at rest and fails closed at runtime (no signing secret → 503; no onboarded workspace → dropped), so a deploy-time gate was redundant with the runtime workspace-onboarding gate. Slack goes live only when an admin onboards a workspace + the secrets are in SSM (§2.4.1). No flag to set.
- **`GatewayPolicyEnforcement`** — `LOG_ONLY` (default) / `ACTIVE`. The fleet Cedar policy's enforcement mode; roll out `LOG_ONLY` first, watch CloudWatch, then flip to `ACTIVE`.
- **`DeployMantleProject`** — `true`/`false` (default `true`). Provisions the fleet's shared model cost-attribution project as `AWS::BedrockMantle::Project` and injects its id as `MANTLE_PROJECT_ID` on every agent runtime. **Prerequisite:** activate the resource type in the account+region once before the first deploy — `aws cloudformation activate-type --type RESOURCE --type-name AWS::BedrockMantle::Project` (see below). One project fleet-wide — a dispatch may act across several repos, so per-repo attribution is meaningless. Set `false` to skip provisioning and bring your own id via `MantleProjectId`.
- **`MantleProjectId`** — a pre-existing Bedrock Mantle project id, used only when `DeployMantleProject=false`. Blank (default) leaves agents on the account's default Mantle project. Ignored when `DeployMantleProject=true`.
- **`AsanaMcpEndpoint`** — Asana MCP server endpoint registered as the direct gateway target (default points at the official server). GitHub has no endpoint parameter: its gateway target is the SCM broker Lambda (`infra/dispatch/scm_broker.py`), not a direct MCP server.
- **`GitHubAppName`** — display name used when registering the fleet's GitHub App from the admin manifest flow (must be unique across GitHub).
- **`RequireAgentApproval`** — `true` (default) / `false`. The agent-authoring approval gate (spec §7.5): when on, a custom agent whose pip requirements or skills are **novel** relative to its last-approved baseline lands `pending_review` and does not build until a **second admin** (enforced distinct from the author) approves it in the dashboard. Deliberately a deploy-time parameter, not a runtime toggle — relaxing it requires deploy rights and is an auditable infra change. Built-in enable/disable never needs approval.

Which GitHub repos the fleet acts on is no longer a deploy parameter. The fleet is multi-repo: deploy it once, then an admin onboards repos at runtime in the dashboard's Admin view (stored in the `fleet-config-${Stage}` DynamoDB table). The Dispatch Router reads that allowlist and rejects a GitHub mention from a non-onboarded repo with a `403`. See § AgentCore Gateway below for the tool-call boundary that complements it.

**If you deploy without the dashboard** (`DeployDashboard=false`), there is no Admin UI to onboard repos — an empty allowlist would reject every GitHub mention. `bootstrap.py` therefore prompts for **initial repos** and seeds them directly into `fleet-config-${Stage}` (enabled + eligible + active). Provide at least one, or onboard later by writing repo rows to that table (`pk="repo#<owner/repo>"`, lowercase). This is the only in-band onboarding path when the dashboard is off.

### 2.3.1 AgentCore Gateway + Cedar policy (the deterministic tool-call boundary)

The fleet is **gateway-only**: all agent tool calls flow through the Gateway, so it is REQUIRED for GitHub (not opt-in) — `DeployGateway=true` (which needs `DeployDashboard=true`). The dispatch allowlist (above) stops a *mention* from a non-onboarded repo; the Gateway + its co-repo interceptor stop a *tool call* against a repo the dispatch isn't approved to run with, and the scoped credential is the backstop below both (threat T-11). Rolled out in stages:

1. **Deploy it** — `DeployGateway=true`, `GatewayPolicyEnforcement=LOG_ONLY`. This creates the policy engine, the Gateway (MCP, `AWS_IAM` inbound), the `AsanaTarget` (direct MCP), the `GitHubTarget` — a **Lambda target** backed by the SCM broker (`scm-broker-${Stage}`) which mints a per-owner, repo-and-permission-scoped GitHub App token per call (the gateway holds no GitHub credential) — and the **REQUEST interceptor** (`scm-interceptor-${Stage}`) that enforces per-origin co-repo grouping from the trusted `x-dispatch-origin` header before the call reaches the broker. `bootstrap.py` / `deploy_fleet.py` provision this when the dashboard is enabled; the `capability-deployer` Lambda grants each agent's runtime role `bedrock-agentcore:InvokeGateway` (and NO GitHub credential) on the fleet gateway when it creates the role.
2. **Route agents through it** — the `capability-deployer` Lambda sets `GATEWAY_MCP_URL` (stack output `FleetGatewayUrl`, resolved from the deployer's own environment) on each agent runtime it deploys. The fleet is **gateway-only**: `GATEWAY_MCP_URL` is REQUIRED — an agent raises at startup without it (there is no direct-to-vendor fallback; that would bypass the policy engine, the co-repo interceptor, and the per-tool observability). Each agent opens a **single** MCP client to the gateway, SigV4-signed with its runtime role (`mcp-proxy-for-aws`, service `bedrock-agentcore`) — no Bearer tokens — and stamps the trusted `x-dispatch-origin` + `x-dispatch-agent` headers (`agents/shared/tools/gateway.py`). The runtime role gets the `InvokeGateway` grant from the deployer's inline policy and holds NO GitHub credential.
3. **Confirm the manifest-dependent bits** against the *live* gateway `tools/list` — run `python scripts/check_gateway_manifest.py --stage <stage>`. It connects with the same SigV4 transport and reports any policy action that isn't in the manifest (a dead clause) and any GitHub write-like tool not covered by a forbid (a gap). For the GitHub target this is largely self-consistent by construction: the broker's tool surface is the curated inline schema generated from `scm_broker.tool_definitions()` (asserted against the template by `infra/dispatch/tests/test_scm_broker.py`), and its tool set is cross-checked against `fleet_policy` (also in that test). Reconcile against `infra/dashboard/fleet_policy.py`:
   - the tool names in `WRITE_TOOLS`, `DESTRUCTIVE_TOOLS`, and `AGENT_TOOL_GRANTS`, and the target names (`GITHUB_TARGET`/`AsanaTarget`) — action ids are `<TargetName>___<toolName>`. `GITHUB_TARGET` stays `GitHubTarget` (the broker target's Name), so the action ids are unchanged from the pre-broker direct target;
   - the repo parameter shape (`REPO_PARAM_MODE` — `pair` for separate `owner`/`repo` inputs, which the broker exposes, `single` for a combined `repo`).
4. **Observe, then enforce** — watch CloudWatch for unexpected `LOG_ONLY` denies, then redeploy with `GatewayPolicyEnforcement=ACTIVE`. On every allowlist change the admin API regenerates the fleet repo-policy AND re-syncs the per-agent permit policies (`infra/dashboard/policy_sync.py`) at whatever enforcement mode is set.

**Policies the admin Lambda owns** (live `bedrock-agentcore-control` calls, not CloudFormation, so admin edits aren't clobbered by stack drift):
- `sdlc_allowed_repos` — the repo-allowlist forbid + unconditional destructive forbid (from the onboarded repo set).
- `sdlc_permit_<agent>` — one per agent, granting that agent's runtime-role principal (`AgentCore::IamEntity::"arn:aws:sts::<acct>:assumed-role/<agent>-agentcore-runtime"`) its allowed `<Target>___<tool>` actions on the literal gateway ARN. **These are required**: the engine is default-deny + forbid-wins, so without permits an `ACTIVE` engine denies every tool call — which is why rollout is `LOG_ONLY` first. The per-agent tool scope is defined in `fleet_policy.AGENT_TOOL_GRANTS` (the enforced form of the intent in `cedar/*.cedar`).

The engine, gateway, targets, and gateway role are the CloudFormation scaffold; all policies are live API calls.

### 2.4 SSM SecureString parameters (populated by connect skills)

The shipping code expects these. Each is written by the corresponding skill or bootstrap script; no agent creates them.

| Parameter | Written by | Consumed by |
|---|---|---|
| `/sdlc-agents/asana-pat` | `sdlc-agents-connect-asana` (Step 1) | `asana-webhook-${Stage}` Lambda (REST calls) |
| `/sdlc-agents/asana-webhook-secret` | `scripts/bootstrap_asana_webhook.py` (operator-run; attaches a temporary inline `ssm:PutParameter` policy to the Lambda role for the handshake window) | `asana-webhook-${Stage}` Lambda (signature verify) |
| `/sdlc-agents/asana-mcp-client-id` | `sdlc-agents-connect-asana` (Step 2) | Agent runtimes reading Asana |
| `/sdlc-agents/asana-mcp-client-secret` | `sdlc-agents-connect-asana` (Step 2) | Agent runtimes reading Asana |
| `/sdlc-agents/asana-mcp-refresh-token` | `scripts/bootstrap_asana_oauth.py` (Step 3 of connect-asana) | Agent runtimes reading Asana |
| `/sdlc-agents/${Stage}/github-app-id` / `...-slug` (SSM String) + `sdlc-agents/${Stage}/github-app/private-key` (Secrets Manager) | `sdlc-agents-connect-github` (dashboard manifest flow writes them) | Agent runtimes, dispatch reply Lambda, and the SCM broker — minting per-owner GitHub App installation tokens. Per-owner `installation_id` is in the `fleet-config-${Stage}` table, not SSM. |
| `/sdlc-agents/researcher-tavily-api-key` | Manual | Researcher's `web_search` tool |
| `/sdlc-agents/${Stage}/registry` | `capability-deployer` Lambda / admin API (rendered from active capability rows on every change) | Dispatch Router Lambda |

Missing any required parameter produces a clear error at invocation time (not at deploy time). `bootstrap.py` preflights these SSM secrets and points you at the bootstrap scripts for any that are missing.

### 2.4.1 Slack SecureString parameters (needed once you onboard a Slack workspace)

Populated out-of-band by `scripts/bootstrap_slack.py` (which also registers the Slack app manifest against the `SlackEventsEndpoint`/`SlackCommandsEndpoint` outputs). Note the level split — it matters for blast radius (threat T-36):

| Parameter | Level | Written by | Consumed by |
|---|---|---|---|
| `/sdlc-agents/${Stage}/slack/signing-secret` | **App-level** (one per Slack app; the `url_verification` handshake carries no team scope, so verification can't depend on a `team_id`) | `scripts/bootstrap_slack.py` | `slack-webhook-${Stage}` Lambda (signature verify) |
| `/sdlc-agents/${Stage}/slack/<team_id>/bot-token` | **Per-workspace/installation** (`xoxb-…`) | `scripts/bootstrap_slack.py` (once per onboarded workspace) | `reply.post_slack_message` — used by the Dispatch Router (replies + fleet-event notifications), the `github-webhook` Lambda (SCM notifications), and the `assignment-notifier` Lambda (terminal-status notifications), all via `notify.py`/`reply.py` |

Both are fetched per-invocation and never held on a module global. The receiver Lambda's IAM grants `ssm:GetParameter` on `/sdlc-agents/${Stage}/slack/*` only; it cannot write them. A workspace is not live until (a) its bot token is in SSM and (b) an admin has onboarded it (an enabled, active `slack_workspace` row) in the dashboard Connectors → Slack panel. (The receiver Lambda itself is always deployed — there's no deploy flag.)

### 2.5 Per-agent runtime environment

Agents no longer get their environment from GitHub Actions variables — there's no deploy workflow to bake them in. Each runtime's env is assembled by the `capability-deployer` Lambda from two sources:

- **Base env** (fleet-wide, from the deployer's own environment, which the stack populates): `BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION`, `GATEWAY_MCP_URL`.
- **Capability env** (per-agent, from the capability row): the `env` map an admin enters in the dashboard Onboard form (`KEY=value, KEY2=value2`). This is where agent-specific values like `GITHUB_REPO`, `ASANA_WORKSPACE_GID`, `ASANA_PROJECT_GID`, and `ASANA_PROJECT_NAME` go. The deployer refuses reserved keys (e.g. the guardrail/gateway keys) so an onboard can't override the base env.

The merged env is applied wholesale on every deploy (the deployer always passes the full set), so editing a capability's env and re-onboarding is how you change a running agent's environment.

### 2.6 GitHub Actions repository secrets

The fleet needs **none** — triggers are the GitHub App webhook and build/deploy is server-side (§1.3). The only GitHub Actions secret/variable in play is for the **optional, separate** Claude Code on Bedrock feature (§1.4), and only in a repo where you enable it:

| Secret / variable | Consumed by |
|---|---|
| `CLAUDE_CODE_ROLE_ARN` (secret) + `CLAUDE_CODE_AWS_REGION` (variable) | `claude-code.yml` in the target repo (only if you run the `sdlc-agents-setup-claude-code` skill) |

## 3. Ordering for a first-time deploy

Top-to-bottom, no skipping.

1. **Enable Bedrock model access** (console) for the fleet's Mantle model (`anthropic.claude-sonnet-5`) in `$AWS_REGION`.
2. **Activate the Mantle project CFN resource type** (once per account+region) — `aws cloudformation activate-type --type RESOURCE --type-name AWS::BedrockMantle::Project --region $AWS_REGION`. Required because the base deploy provisions `AWS::BedrockMantle::Project` by default (`DeployMantleProject=true`). Skip only if you set `DeployMantleProject=false`. See § 2.2.
3. **Deploy the base platform** — `python scripts/deploy_fleet.py --stage <stage> --region <region>` (or the interactive `scripts/bootstrap.py`). This runs `sam deploy` for the foundation stack (Dispatch Router, webhook Lambda, API Gateway, DynamoDB, S3, SSM registry parameter, guardrail, the shared build pipeline + capability deployer/rebuilder, and — when enabled — Cognito + dashboard API/CDN + Gateway), uploads the agent build source, and publishes the dashboard SPA.
   - **Full solution in one command:** add `--full` to bring up the complete stack (`DeployDashboard=true`, `DeployGateway=true`, `GatewayPolicyEnforcement=LOG_ONLY`, `DeployMantleProject=true`, `RequireAgentApproval=true`) in a single deploy — e.g. `python scripts/deploy_fleet.py --stage staging --region us-east-1 --full`. Without `--full` (or explicit `--param` flags) a **new** stack comes up with the template defaults, which have the dashboard and gateway **off**.
   - **Individual parameters:** `--param KEY=VALUE` (repeatable) sets any foundation parameter — e.g. `--param DeployDashboard=true --param WorkitemsBotGID=1201234567890`. On a redeploy each `--param` overrides that key's preserved value while every other parameter carries forward, so flipping one toggle (e.g. `--param GatewayPolicyEnforcement=ACTIVE` once you've watched LOG_ONLY) never silently resets the rest. `DeployGateway=true` requires `DeployDashboard=true` (the admin API owns the Cedar policy sync); the CLI fails fast if you pass one without the other.
   - By default `sam` prints the changeset and prompts before applying IAM/networking changes; pass `--auto-approve` for an unattended run.
4. **Add dashboard operators/admins** — create Cognito users and add them to the `operators` (view) and `admins` (onboard) groups. Sign in at the `DashboardUrl` output.
5. **Connect integrations** — run `sdlc-agents-connect-asana` and/or `sdlc-agents-connect-github` to populate SSM parameters.
6. **Onboard each agent** — in the dashboard Admin view's Capabilities panel, onboard each agent (`agent_id` matching a directory under `agents/` in the build source, plus optional description/aliases/env). Each onboard builds the container, stands up the runtime role + AgentCore runtime, waits READY, and republishes the registry.
7. **Onboard the repos the fleet may act in** — in the Admin view's repo panel (or `bootstrap.py`'s initial-repo seed when the dashboard is off). Mentions from a non-onboarded repo are rejected at dispatch.
8. **Author trigger authorization** — no dispatch is authorized until a grant exists (the `TriggerPolicyStore` is default-deny; the old `authorization.users` allowlist is gone). Add `trigger_rule` grants in the dashboard Connectors → Trigger Rules panel (or via `sdlc-agents-register-triggers`), keyed on immutable sender principals (`github:<login>`, `asana:<gid>`, `slack:<team>:<uid>`).
9. **Register the Asana webhook** — `sdlc-agents-register-triggers` calls the Asana API with the `WebhookEndpoint` stack output.
10. **(Optional) Enable Slack** — the receiver is already deployed; run `scripts/bootstrap_slack.py` to write the app signing secret + per-workspace bot token and register the Slack app manifest against the `SlackEventsEndpoint`/`SlackCommandsEndpoint`/`SlackInteractionsEndpoint` outputs, then onboard each workspace/channel in the dashboard Connectors → Slack panel. Users self-serve notifications with `/sdlc-notify`.
11. **Verify** — `sdlc-agents-verify` runs layered smoke tests (runtime health → credential freshness → end-to-end mention).

Everything except the SSM secrets (populated by the connect skills) and the Asana webhook registration is driven by the base deploy plus dashboard onboarding.

## 4. What a "deterministic" deploy requires that we don't have today

Gaps a team running the fleet at volume will hit:

1. **SSM parameter creation is manual.** The secure parameters (Asana PAT, MCP credentials, GitHub App key, Tavily key) are one-shot bootstraps. That's fine — they're secrets — but the platform can't tell at-deploy-time whether they're present. `bootstrap.py` preflights them and points you at the bootstrap scripts for any that are missing.
2. **AgentCore Memory isn't provisioned.** Agents honor `AGENTCORE_MEMORY_ID` if set; if you want Memory, you create the resource and set the env var (via the capability's env) yourself. Should be an opt-in parameter on the foundation stack.
3. **No Cedar evaluator outside the Gateway path.** The `cedar/*.cedar` files are advisory unless the AgentCore Gateway is deployed; the enforced form lives in `infra/dashboard/fleet_policy.py`. A deterministic deploy would include a verifier step that fails if Cedar syntax is broken.

None of these block adoption today.

## 5. Resources you destroy for a clean teardown

Rough shutdown order:

1. De-register the Asana webhook (`curl DELETE https://app.asana.com/api/1.0/webhooks/<id>`).
2. Delete each onboarded agent's AgentCore Runtime (`bedrock-agentcore-control delete-agent-runtime`).
3. Delete each agent's IAM runtime role (under path `/sdlc-agents/capabilities/`).
4. Delete each `sdlc-agents/<agent>` ECR repository (including all images).
5. Delete the foundation CloudFormation stack (`sam delete`). This removes the Dispatch Router, the webhook Lambdas (GitHub, Asana, and Slack when enabled), API Gateway, the `TriggerPolicyStore` (AVP), DynamoDB tables (`dispatch-assignments`, `fleet-config` — the latter holding the trigger-rule/Slack data rows), S3 buckets (must be empty first — including the build-source, dashboard, and skills buckets), SSM registry parameter, the build pipeline + capability deployer/rebuilder + skill unpacker, guardrail, and CloudWatch alarms.
6. Delete the SSM parameters (`asana-*`, `github-app-*`, `researcher-tavily-api-key`, and — if Slack was enabled — everything under `/sdlc-agents/${Stage}/slack/*`) and the Secrets Manager `sdlc-agents/github-app/private-key` secret.
7. If Slack was enabled, delete the Slack app (or its webhook subscriptions) from the Slack side so it stops sending deliveries.
8. If you enabled the optional Claude Code on Bedrock feature (§1.4), delete its `ClaudeCodeBedrockRole` and the GitHub OIDC provider/trust you created for it. (The fleet itself creates no OIDC provider or CI role to clean up.)
9. Disable Bedrock model access (optional).

S3 bucket deletion blocks on non-empty. Explicit empty before destroy is required.

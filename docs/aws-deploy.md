# AWS Deploy Surface

What this project provisions in AWS, and what inputs you need to make a deploy deterministic from scratch.

## 1. What gets deployed

Everything lives in a single AWS account + region. There are three layers of resources:

### 1.1 Foundation stack (`infra/foundation/template.yaml`)

Deployed once per stage with `sam deploy`. Creates:

| Resource | Logical name | Purpose |
|---|---|---|
| DynamoDB table | `dispatch-assignments-${Stage}` | Assignment tracking (PK `assignment_id`; GSIs on `agent_id+status`, `source+created_at`); 30-day TTL |
| S3 bucket | `sdlc-agent-artifacts-${AWS::AccountId}-${Stage}` | Agent output artifacts (screenshots, test results); SSE-AES256; lifecycle rules on `screenshots/` (90d) and `test-results/` (180d) |
| Lambda | `dispatch-router-${Stage}` | Parses `@mentions`, checks auth, invokes the right AgentCore Runtime, writes assignment to DynamoDB |
| Lambda | `asana-webhook-${Stage}` | Verifies Asana webhook signatures, normalizes events, invokes Dispatch Router async |
| API Gateway | `WebhookApi` | Fronts the Asana webhook Lambda at `/asana/webhook` |
| SSM parameter | `/sdlc-agents/${Stage}/registry` | Dispatch Router registry, rendered from the active capability rows in `fleet-config-${Stage}` (re-written on every capability change by the admin API / capability deployer) |
| CloudWatch alarms | Three alarms | Dispatch error rate, webhook error rate, dispatch p99 duration |

**Outputs:** `AssignmentsTableName`, `ArtifactsBucketName`, `DispatchRouterArn`, `WebhookEndpoint` (Asana webhook URL), `WebhookApiId`.

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

### 1.2 Per-agent runtime (created by UI onboarding, not SAM, not CI)

Agents are onboarded from the dashboard Admin view (the **Capabilities** panel). Onboarding writes a capability row to `fleet-config-${Stage}` and drives the rest of the lifecycle through the shared build pipeline and the capability deployer — no per-agent script, workflow, or manual command:

| Resource | Logical name pattern | Created by |
|---|---|---|
| ECR repository | `sdlc-agents/<agent>` | The shared `sdlc-agent-builder-${Stage}` CodeBuild buildspec (idempotent `ecr describe-repositories` or `create-repository`) |
| Container image | `sdlc-agents/<agent>:<build-tag>` in ECR (repos are `IMMUTABLE` — one tag per build, no `:latest`) | `sdlc-agent-builder-${Stage}` CodeBuild (parameterized by `AGENT_NAME`, building `agents/<agent>` from the uploaded build source) |
| IAM role | `<agent>-agentcore-runtime` (under IAM path `/sdlc-agents/capabilities/`) | `capability-deployer-${Stage}` Lambda, created with a mandatory permissions boundary |
| AgentCore Runtime | `<agent>` | `capability-deployer-${Stage}` Lambda, on the build-completion EventBridge event |

The flow: the admin API starts one shared build (it holds only `codebuild:StartBuild`); on build completion an EventBridge rule invokes the `capability-deployer` Lambda (the only component holding `iam:CreateRole`/`PassRole` + `create/update-agent-runtime`), which ensures the runtime role, deploys the runtime, waits for READY, marks the capability active, and republishes the registry. A weekly EventBridge schedule invokes the `capability-rebuilder` Lambda, which re-runs the same build for every active capability (security patching); a failed build or deploy never tears down a working runtime.

Four agents ship in `agents/` today: `workitems`, `researcher`, `docwriter`, `adr`. Each becomes per-agent runtime surface once onboarded.

### 1.3 GitHub Actions OIDC + role (for `@mention` dispatch and `@claude`)

There is **no agent-deploy CI role** — the OIDC provider, CI deploy role, and per-agent runtime roles that the old deploy workflows depended on have been retired. The two remaining GitHub Actions workflows that call AWS — `agent-dispatch.yml` (routes `@agent` mentions by invoking the Dispatch Router / AgentCore) and `claude-code.yml` (`@claude`) — still authenticate to AWS via OIDC using the `AWS_DEPLOY_ROLE_ARN` secret. If you use them, that role needs:

- Trust policy allowing `token.actions.githubusercontent.com`, with `sub` restricted via `StringEquals` to the exact subjects your workflows use. For this repo that's two subjects: `repo:<your-org>/<your-repo>:ref:refs/heads/main` (covers the comment-driven triggers like `issue_comment` / `pull_request_review_comment` in `agent-dispatch.yml` and `claude-code.yml` — all of which run on the default branch) and `repo:<your-org>/<your-repo>:pull_request` (covers the `pull_request: [opened, synchronize]` trigger in `claude-code.yml`, which auto-reviews new PRs). Do **not** use `StringLike: "repo:<org>/<repo>:*"` — that allows any branch, tag, or environment in the repo to assume the role, including feature branches a contributor can push without review. Re-check this list if you add workflows that use `workflow_dispatch`, `schedule`, or `workflow_call` from a different repo — those may emit different `sub` claims.
- A scoped permission policy for what those workflows actually do: `bedrock-agentcore:InvokeAgentRuntime` (dispatch), `bedrock:InvokeModel`/`ApplyGuardrail` (Claude Code on Bedrock), and any read the dispatch step needs. It does **not** need ECR push, `iam:PassRole`, `create/update-agent-runtime`, `ssm:PutParameter`, or CloudFront/S3 publish — none of the agent build/deploy or dashboard publish happens in CI anymore.
- OIDC provider for `token.actions.githubusercontent.com` with `sts.amazonaws.com` audience and the GitHub thumbprint

The role ARN goes into the target repo's GitHub Actions secrets as `AWS_DEPLOY_ROLE_ARN`. The base platform itself (`scripts/deploy_fleet.py` / `scripts/bootstrap.py`) runs with a privileged human's own credentials — it does not use this role.

### 1.4 Optional: Claude Code on Bedrock (one-time, per repo)

If you use the `sdlc-agents-setup-claude-code` skill, it creates:

- IAM role `ClaudeCodeBedrockRole` with `bedrock:InvokeModel` on Opus 4.7's inference profile
- Secret `CLAUDE_CODE_ROLE_ARN` + variable `CLAUDE_CODE_AWS_REGION` on the target repo

Independent of the fleet — you can deploy it or not.

## 2. Inputs required for a deterministic deploy

### 2.1 AWS account + region

- **`AWS_ACCOUNT_ID`** — 12-digit account ID, stored as a GitHub Actions secret.
- **`AWS_REGION`** — deployment region, stored as a GitHub Actions variable. Must have Bedrock model access enabled for `us.anthropic.claude-opus-4-7-v1`.

### 2.2 Bedrock model access

Enabled in the Bedrock console → Model access. Required in the same region as `AWS_REGION`. Without this, agent invocations return `AccessDeniedException`.

### 2.3 SAM parameters (for the foundation stack)

Passed to `sam deploy --parameter-overrides`:

- **`Stage`** — `dev` / `staging` / `prod`. Embedded in every resource name.
- **`WorkitemsBotGID`** — Asana user GID that tasks are assigned to to trigger Workitems.
- **`AgentFieldGID`** — Asana custom field GID for the "Agent" dropdown (optional — empty string is fine if you're not using custom-field triggers).
- **`DeployDashboard`** — `true`/`false` (default `false`). Provisions the Cognito user pool, the operator/admin read+write APIs, and the CloudFront SPA.
- **`DeployGateway`** — `true`/`false` (default `false`). Provisions the AgentCore Gateway + Cedar policy engine (the deterministic tool-call boundary). Requires `DeployDashboard=true` (the admin API owns the policy sync).
- **`GatewayPolicyEnforcement`** — `LOG_ONLY` (default) / `ACTIVE`. The fleet Cedar policy's enforcement mode; roll out `LOG_ONLY` first, watch CloudWatch, then flip to `ACTIVE`.
- **`AsanaMcpEndpoint`** — Asana MCP server endpoint registered as the direct gateway target (default points at the official server). GitHub has no endpoint parameter: its gateway target is the SCM broker Lambda (`infra/dispatch/scm_broker.py`), not a direct MCP server.
- **`GitHubAppName`** — display name used when registering the fleet's GitHub App from the admin manifest flow (must be unique across GitHub).

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

### 2.5 Per-agent runtime environment

Agents no longer get their environment from GitHub Actions variables — there's no deploy workflow to bake them in. Each runtime's env is assembled by the `capability-deployer` Lambda from two sources:

- **Base env** (fleet-wide, from the deployer's own environment, which the stack populates): `BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION`, `GATEWAY_MCP_URL`.
- **Capability env** (per-agent, from the capability row): the `env` map an admin enters in the dashboard Onboard form (`KEY=value, KEY2=value2`). This is where agent-specific values like `GITHUB_REPO`, `ASANA_WORKSPACE_GID`, `ASANA_PROJECT_GID`, and `ASANA_PROJECT_NAME` go. The deployer refuses reserved keys (e.g. the guardrail/gateway keys) so an onboard can't override the base env.

The merged env is applied wholesale on every deploy (the deployer always passes the full set), so editing a capability's env and re-onboarding is how you change a running agent's environment.

### 2.6 GitHub Actions repository secrets

Only the surviving GitHub event workflows (`agent-dispatch.yml`, `claude-code.yml`) use these; there is no agent-deploy CI anymore:

| Secret | Consumed by |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `agent-dispatch.yml`, `claude-code.yml` (OIDC role for dispatch invoke / Claude Code on Bedrock — see §1.3) |
| `CLAUDE_CODE_ROLE_ARN` | `claude-code.yml` (optional — only if using a dedicated Claude Code on Bedrock role) |

`agent-dispatch.yml` and `claude-code.yml` also read the repository variables `AWS_REGION` (falls back to `us-west-2`) and `CLAUDE_CODE_AWS_REGION` (falls back to `us-east-1`).

## 3. Ordering for a first-time deploy

Top-to-bottom, no skipping.

1. **Enable Bedrock model access** (console) for `us.anthropic.claude-opus-4-7-v1` in `$AWS_REGION`.
2. **Deploy the base platform** — `python scripts/deploy_fleet.py --stage <stage> --region <region>` (or the interactive `scripts/bootstrap.py`), with the dashboard enabled (`DeployDashboard=true`; add `DeployGateway=true` for the tool-call boundary). This runs `sam deploy` for the foundation stack (Dispatch Router, webhook Lambda, API Gateway, DynamoDB, S3, SSM registry parameter, guardrail, the shared build pipeline + capability deployer/rebuilder, and — when enabled — Cognito + dashboard API/CDN + Gateway), uploads the agent build source, and publishes the dashboard SPA.
3. **Add dashboard operators/admins** — create Cognito users and add them to the `operators` (view) and `admins` (onboard) groups. Sign in at the `DashboardUrl` output.
4. **Connect integrations** — run `sdlc-agents-connect-asana` and/or `sdlc-agents-connect-github` to populate SSM parameters.
5. **Onboard each agent** — in the dashboard Admin view's Capabilities panel, onboard each agent (`agent_id` matching a directory under `agents/` in the build source, plus optional description/aliases/env). Each onboard builds the container, stands up the runtime role + AgentCore runtime, waits READY, and republishes the registry.
6. **Onboard the repos the fleet may act in** — in the Admin view's repo panel (or `bootstrap.py`'s initial-repo seed when the dashboard is off). Mentions from a non-onboarded repo are rejected at dispatch.
7. **Register the Asana webhook** — `sdlc-agents-register-triggers` calls the Asana API with the `WebhookEndpoint` stack output.
8. **Verify** — `sdlc-agents-verify` runs layered smoke tests (runtime health → credential freshness → end-to-end mention).

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
5. Delete the foundation CloudFormation stack (`sam delete`). This removes the Dispatch Router, webhook Lambda, API Gateway, DynamoDB tables, S3 buckets (must be empty first — including the build-source and dashboard buckets), SSM registry parameter, the build pipeline + capability deployer/rebuilder, guardrail, and CloudWatch alarms.
6. Delete the SSM SecureString parameters (`asana-*`, `github-*`, `researcher-tavily-api-key`).
7. Delete the GitHub Actions OIDC role/provider if you set one up for `agent-dispatch.yml` / `claude-code.yml` and no longer need it.
8. Disable Bedrock model access (optional).

S3 bucket deletion blocks on non-empty. Explicit empty before destroy is required.

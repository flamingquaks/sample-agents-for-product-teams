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
| SSM parameter | `/sdlc-agents/${Stage}/registry` | Agent registry, populated by `scripts/sync_registry.py` from `.dispatch/agents.yaml` |
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
`.github/workflows/deploy-dashboard.yml` (build → write `config.json` from these
outputs → S3 sync → CloudFront invalidation). Operators are created by an admin
(no self sign-up) and must be added to the `operators` group. See
`dashboard/README.md`.

### 1.2 Per-agent runtime (created by the deploy pipeline, not SAM)

Each agent creates its own AWS resources when its deploy workflow runs for the first time:

| Resource | Logical name pattern | Created by |
|---|---|---|
| ECR repository | `sdlc-agents/<agent>` | `.github/workflows/deploy-agent.yml` (idempotent `ecr describe-repositories` or `create-repository`) |
| IAM role | `<agent>-agentcore-runtime` | **Not automated today.** Must be created manually or via the `sdlc-agents-provision-aws` skill. |
| AgentCore Runtime | `<agent>` | `.github/workflows/deploy-agent.yml` on first push |
| Container image | `sdlc-agents/<agent>:<commit-sha>` in ECR (repos are `IMMUTABLE` — one tag per build, no `:latest`) | `.github/workflows/deploy-agent.yml` |

Four agents ship today, so the per-agent surface is **four of each** of the above: `workitems`, `researcher`, `docwriter`, `adr`.

### 1.3 OIDC + deploy role (one-time)

> **The interactive bootstrap (`scripts/bootstrap.py`) creates all of this for you** — the OIDC provider, the deploy role with the repo-scoped trust below, and a *scoped* permission policy (not `AdministratorAccess`) matching what the CI workflows actually do (ECR push, AgentCore runtime create/update + invoke, `iam:PassRole` on the `*-agentcore-runtime` roles, `ssm:PutParameter` on the registry, `cloudformation:DescribeStacks`, Bedrock invoke, and — when the dashboard is enabled — S3 + CloudFront publish). The manual details below are for provisioning it by hand. Note the *bootstrapper's own* credentials (a privileged human) still need broad rights to run `sam deploy` and create IAM roles; that breadth is not granted to the CI role.

The GitHub Actions workflows assume an IAM role via OIDC. This is **not created by the foundation stack.** The bootstrap script creates it; to do it by hand, the role needs:

- Trust policy allowing `token.actions.githubusercontent.com`, with `sub` restricted via `StringEquals` to the exact subjects your workflows use. For this repo that's two subjects: `repo:<your-org>/<your-repo>:ref:refs/heads/main` (covers `push`-to-main events for the deploy workflows, and comment-driven triggers like `issue_comment` / `pull_request_review_comment` in `agent-dispatch.yml` and `claude-code.yml` — all of which run on the default branch) and `repo:<your-org>/<your-repo>:pull_request` (covers the `pull_request: [opened, synchronize]` trigger in `claude-code.yml`, which auto-reviews new PRs). Do **not** use `StringLike: "repo:<org>/<repo>:*"` — that allows any branch, tag, or environment in the repo to assume the role, including feature branches a contributor can push without review. Re-check this list if you add workflows that use `workflow_dispatch`, `schedule`, or `workflow_call` from a different repo — those may emit different `sub` claims.
- A scoped permission policy (what `scripts/bootstrap.py` attaches as the inline `sdlc-agents-deploy` policy): ECR push/pull, `bedrock-agentcore-control:*` + `bedrock-agentcore:InvokeAgentRuntime`, `iam:PassRole` limited to the `*-agentcore-runtime` roles (passed to `bedrock-agentcore.amazonaws.com`), `ssm:PutParameter`/`GetParameter` for the registry, `cloudformation:DescribeStacks` for reading outputs, `bedrock:InvokeModel`/`ApplyGuardrail`, and — when the dashboard is enabled — `s3:PutObject`/`DeleteObject`/`ListBucket` on the dashboard bucket + `cloudfront:CreateInvalidation`. **The CI role does not create the foundation stack** — that `sam deploy` runs locally in the bootstrap script, so the CI role needs no CloudFormation-write or IAM-role-create permissions.
- OIDC provider for `token.actions.githubusercontent.com` with `sts.amazonaws.com` audience and the GitHub thumbprint

The role ARN goes into the target repo's GitHub Actions secrets as `AWS_DEPLOY_ROLE_ARN`.

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
- **`GitHubMcpEndpoint`** / **`AsanaMcpEndpoint`** — MCP server endpoints registered as gateway targets (defaults point at the official servers).

Which GitHub repos the fleet acts on is no longer a deploy parameter. The fleet is multi-repo: deploy it once, then an admin onboards repos at runtime in the dashboard's Admin view (stored in the `fleet-config-${Stage}` DynamoDB table). The Dispatch Router reads that allowlist and rejects a GitHub mention from a non-onboarded repo with a `403`. See § AgentCore Gateway below for the tool-call boundary that complements it.

**If you deploy without the dashboard** (`DeployDashboard=false`), there is no Admin UI to onboard repos — an empty allowlist would reject every GitHub mention. `bootstrap.py` therefore prompts for **initial repos** and seeds them directly into `fleet-config-${Stage}` (enabled + eligible + active). Provide at least one, or onboard later by writing repo rows to that table (`pk="repo#<owner/repo>"`, lowercase). This is the only in-band onboarding path when the dashboard is off.

### 2.3.1 AgentCore Gateway + Cedar policy (the deterministic tool-call boundary)

The dispatch allowlist (above) stops a *mention* from a non-onboarded repo. The Gateway stops a *tool call* against a non-allowlisted repo — even one an over-scoped GitHub PAT could otherwise reach (threat T-11). It is opt-in and rolled out in stages:

1. **Deploy it** — `DeployGateway=true`, `GatewayPolicyEnforcement=LOG_ONLY`. This creates the policy engine, the Gateway (MCP, `AWS_IAM` inbound), and the `GitHubTarget`/`AsanaTarget` MCP targets. `bootstrap.py` offers this when the dashboard is enabled, and grants each agent's runtime role `bedrock-agentcore:InvokeGateway` on the fleet gateway.
2. **Route agents through it** — set `GATEWAY_MCP_URL` (stack output `FleetGatewayUrl`) on each agent runtime. Absent this env, agents connect direct to the vendor MCP servers (pre-gateway behavior), so this is a deliberate, reversible switch. The agent code handles the switch: with `GATEWAY_MCP_URL` set, each agent opens a **single** MCP client to the gateway, SigV4-signed with its runtime role (`mcp-proxy-for-aws`, service `bedrock-agentcore`) — no Bearer tokens — via `agents/shared/tools/gateway.py`. The runtime role needs the `InvokeGateway` grant from step 1 (bootstrap adds it).
3. **Confirm the manifest-dependent bits** against the *live* gateway `tools/list` — run `python scripts/check_gateway_manifest.py --stage <stage>`. It connects with the same SigV4 transport and reports any policy action that isn't in the manifest (a dead clause) and any GitHub write-like tool not covered by a forbid (a gap). Reconcile against `infra/dashboard/fleet_policy.py`:
   - the tool names in `WRITE_TOOLS`, `DESTRUCTIVE_TOOLS`, and `AGENT_TOOL_GRANTS`, and the target names (`GITHUB_TARGET`/`AsanaTarget`) — action ids are `<TargetName>___<toolName>`;
   - the repo parameter shape (`REPO_PARAM_MODE` — `pair` for separate `owner`/`repo` inputs, `single` for a combined `repo`).
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
| `/sdlc-agents/github-mcp-token` | `sdlc-agents-connect-github` (PAT path) | Agent runtimes reading GitHub |
| `/sdlc-agents/github-app-id` / `...-installation-id` / `...-private-key` | `sdlc-agents-connect-github` (App path) | Agent runtimes reading GitHub (App auth alternative to PAT) |
| `/sdlc-agents/researcher-tavily-api-key` | Manual | Researcher's `web_search` tool |
| `/sdlc-agents/${Stage}/registry` | `scripts/sync_registry.py` (writes resolved ARNs) | Dispatch Router Lambda |

Missing any required parameter produces a clear error at invocation time (not at deploy time). The deploy workflow's guard catches missing GitHub Actions **variables**, not SSM parameters.

### 2.5 GitHub Actions repository variables

Per `deploy-agent.yml`'s `env_vars` input, the following are baked into each AgentCore Runtime's environment and the workflow fails fast if any required value is empty:

| Variable | Required by | Notes |
|---|---|---|
| `AWS_REGION` | All deploy workflows | Falls back to `us-west-2` if unset |
| `TARGET_REPO` | `deploy-workitems.yml`, `deploy-docwriter.yml`, `deploy-adr.yml` | Format `<owner>/<repo>`. GitHub rejects user-defined variables starting with `GITHUB_`, so the repo variable is `TARGET_REPO` and the deploy workflow passes it through to the container as env var `GITHUB_REPO`. |
| `ASANA_PROJECT_GID` | `deploy-workitems.yml`, `deploy-docwriter.yml`, `deploy-researcher.yml` | |
| `ASANA_WORKSPACE_GID` | same three | |
| `ASANA_PROJECT_NAME` | optional | Cosmetic label in system prompts |
| `CLAUDE_CODE_AWS_REGION` | `claude-code.yml` (optional) | Defaults to `us-east-1` |

### 2.6 GitHub Actions repository secrets

| Secret | Consumed by |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | All deploy workflows, `agent-dispatch.yml` |
| `AWS_ACCOUNT_ID` | All deploy workflows |
| `CLAUDE_CODE_ROLE_ARN` | `claude-code.yml` (optional — only if using Claude Code on Bedrock) |

## 3. Ordering for a first-time deploy

Top-to-bottom, no skipping.

1. **Enable Bedrock model access** (console) for `us.anthropic.claude-opus-4-7-v1` in `$AWS_REGION`.
2. **Create the OIDC provider and deploy role** (skill: `sdlc-agents-provision-aws`, Step 0 prereqs). Capture the role ARN.
3. **Set GitHub Actions secrets** (`AWS_DEPLOY_ROLE_ARN`, `AWS_ACCOUNT_ID`) and variables (`AWS_REGION`, plus per-agent vars).
4. **Deploy the foundation stack** with `sam deploy` — this is how you get the Dispatch Router, webhook Lambda, API Gateway, DynamoDB, S3, SSM registry parameter, and CloudWatch alarms.
5. **Connect integrations** — run `sdlc-agents-connect-asana` and/or `sdlc-agents-connect-github` to populate SSM parameters.
6. **Provision per-agent IAM runtime roles** — `sdlc-agents-provision-aws` creates one per agent, attaches the per-agent SSM-read policy.
7. **Push to `main`** — this is the first time `deploy-agent.yml` runs for each agent. It creates the ECR repo, builds the image, creates the AgentCore Runtime, syncs the registry to SSM, and smoke-tests.
8. **Register the Asana webhook** — `sdlc-agents-register-triggers` calls the Asana API with the `WebhookEndpoint` stack output.
9. **Verify** — `sdlc-agents-verify` runs layered smoke tests (runtime health → credential freshness → end-to-end mention).

Steps 2, 6 are the two that are **not** covered by SAM or CI/CD. The rest are automated once prerequisites are in place.

## 4. What a "deterministic" deploy requires that we don't have today

Gaps to close before a clean `sam deploy && gh workflow run` from a fresh clone produces a live fleet:

1. **Foundation stack doesn't create the OIDC provider or deploy role.** You create them by hand (or via the skill) before the first SAM deploy. These should be part of a bootstrap stack (`infra/bootstrap/template.yaml`) that runs once per account, using long-lived credentials.
2. **Per-agent IAM runtime roles aren't in SAM.** They're created ad-hoc by the skill. They should live in the foundation stack (or a per-agent sub-stack) so `sam deploy` produces them deterministically. Bonus: Cedar policies could move from `cedar/*.cedar` files into the IAM role definition too, or into a dedicated Cedar-authorization resource.
3. **SSM parameter creation is manual.** The secure parameters (Asana PAT, MCP credentials, GitHub PAT, Tavily key) are one-shot bootstraps. That's fine — they're secrets — but today the deploy pipeline can't tell at-deploy-time whether they're present. Adding a pre-deploy check (similar to the `env_vars` guard) would catch this earlier.
4. **AgentCore Memory isn't provisioned.** Agents honor `AGENTCORE_MEMORY_ID` if set; if you want Memory, you create the resource and set the env var yourself. Should be an opt-in parameter on the foundation stack.
5. **No Cedar evaluator in the invocation path.** The `cedar/*.cedar` files are advisory — a deterministic deploy includes a verifier step that fails if Cedar syntax is broken. (Today we could pass syntactically invalid Cedar without noticing.)
6. **Registry sync runs as a deploy-time side effect of each agent push.** `sync_registry.py` requires every agent referenced in `.dispatch/agents.yaml` to have a runtime ARN already resolvable. First-time deploys that touch multiple agents can race. Either serialize agent deploys or teach `sync_registry.py` to tolerate unresolved placeholders.

None of these block adoption today — they're roughness that a team running the fleet at volume will hit first.

## 5. Resources you destroy for a clean teardown

Rough shutdown order:

1. De-register the Asana webhook (`curl DELETE https://app.asana.com/api/1.0/webhooks/<id>`).
2. Delete the four AgentCore Runtimes (`bedrock-agentcore-control delete-agent-runtime`).
3. Delete the four IAM runtime roles.
4. Delete the four ECR repositories (including all images).
5. Delete the foundation CloudFormation stack (`sam delete`). This removes the Dispatch Router, webhook Lambda, API Gateway, DynamoDB table, S3 bucket (must be empty first), SSM registry parameter, and CloudWatch alarms.
6. Delete the SSM SecureString parameters (`asana-*`, `github-*`, `researcher-tavily-api-key`).
7. Delete the OIDC provider and deploy role (if not needed for another stack).
8. Disable Bedrock model access (optional).

S3 bucket deletion blocks on non-empty. Explicit empty before destroy is required.

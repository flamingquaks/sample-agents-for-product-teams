# PDLC Agent Fleet — Setup Guide for AI Agents

This guide is written for an AI agent tasked with deploying this fleet into a new repository and AWS environment. Follow it top to bottom. Where a decision is required, stop and ask the user before proceeding.

---

## Step 0 — Understand what this fleet is

This repo contains autonomous AI agents for the software development lifecycle, deployed as containers on **Amazon Bedrock AgentCore Runtime**. A **Dispatch Router** Lambda receives `@mention` events from GitHub, Asana, and Slack, resolves them to agents, and invokes the appropriate AgentCore Runtime.

**Agents available:**

| Agent | Role | Trigger aliases |
|-------|------|-----------------|
| `workitems` | PO/PM — work decomposition, status reports, risk | `@pm`, `@status`, `@plan` |
| `docwriter` | Technical writer — API docs, guides, release notes | `@docs`, `@doc`, `@writer` |
| `researcher` | Business analyst — research, competitive intel | `@ba`, `@research`, `@analyze` |
| `adr` | ADR linker — tags issues and reviews PRs against the ADR library | `@decisions`, `@architecture` |

The fleet moved to **UI-driven onboarding**. You no longer edit a registry file, write per-agent deploy workflows, or run manual ECR/runtime commands. Instead:

1. **Deploy the base platform once** (`scripts/deploy_fleet.py`) — the foundation stack, the shared build pipeline, and the dashboard SPA.
2. **Onboard each agent from the dashboard Admin view** — the dashboard builds its container and stands up its AgentCore runtime for you.

An onboarded agent is a **capability** row in the `fleet-config-${STAGE}` DynamoDB table. Onboarding builds the agent's container (one shared CodeBuild project, parameterized by `AGENT_NAME`), creates the per-agent runtime IAM role + AgentCore runtime (the `capability-deployer` Lambda), waits for it to become READY, and marks the capability active — at which point the Dispatch Router registry is re-rendered from the capability rows and the agent becomes routable.

---

## Step 1 — Ask the user which agents to deploy

**Stop here. Ask the user:**

> Which agents do you want to deploy? Options are:
> - `workitems` (PO/PM assistant)
> - `docwriter` (technical writer)
> - `researcher` (business analyst)
> - `adr` (ADR linker)
>
> You can deploy any combination. Each one becomes a capability you onboard from the dashboard; onboarding builds its container and stands up an AgentCore Runtime.

Record the user's answer. For the rest of this guide, replace `<AGENTS>` with the chosen list (e.g. `workitems docwriter`).

---

## Step 2 — Collect required values

Before touching any AWS resources, collect the following. Ask the user for any you don't have.

**AWS:**
- `AWS_ACCOUNT_ID` — 12-digit AWS account ID
- `AWS_REGION` — deployment region (default: `us-west-2`; must have Bedrock model access)
- `STAGE` — environment name: `dev`, `staging`, or `prod` (default: `dev`)

**Asana** (only if the user wants Asana triggers — ask):
- `ASANA_PAT` — Personal Access Token for the Asana service account
- `ASANA_WORKSPACE_GID` — Workspace GID (find at `app.asana.com/api/1.0/workspaces`)
- `WORKITEMS_BOT_GID` — Asana user GID for the Workitems bot account (if deploying Workitems)
- `AGENT_FIELD_GID` — GID of the "Agent" custom field on Asana tasks (create it if it doesn't exist)
- Bot GIDs for any other agents being deployed (`DOCWRITER_BOT_GID`, `RESEARCHER_BOT_GID`)

**Confirm before proceeding.** Summarize what you collected and ask: "Does this look right?"

---

## Step 3 — Deploy the base platform

### 3a. Verify Bedrock model access

```bash
aws bedrock get-foundation-model \
  --model-identifier us.anthropic.claude-opus-4-7-v1 \
  --region $AWS_REGION
```

If this returns a `ResourceNotFoundException`, the user must request access in the AWS Bedrock console under **Model access** before continuing.

### 3b. Deploy the base platform

The base platform is everything the UI onboarding runs on: the foundation stack (DynamoDB, Dispatch Router, webhook API, guardrail, SSM registry, the shared build pipeline + capability deployer/rebuilder, and — when enabled — Cognito + the dashboard API/CDN and the AgentCore Gateway), the uploaded agent build source, and the dashboard SPA.

```bash
python scripts/deploy_fleet.py --stage $STAGE --region $AWS_REGION
```

This runs `sam build`/`sam deploy` on `infra/foundation`, zips `agents/` and uploads it as `source.zip` to the build pipeline's source bucket, then builds and publishes the dashboard SPA. It is idempotent; re-runs preserve the stack's existing parameter values (so a redeploy is a code/template update, not a silent reconfiguration). Use `--dry-run` to preview every command first.

The dashboard and AgentCore Gateway are **off by default**. To onboard agents from the Admin UI you need the dashboard, so a first deploy usually turns it on. Set the parameters on the SAM deploy the first time (or with a `sam deploy` of your own), for example `DeployDashboard=true` and, for the tool-call boundary, `DeployGateway=true`. See `docs/aws-deploy.md` §2.3 for the full parameter list.

Alternatively, `python scripts/bootstrap.py` is a thin, interactive one-time base setup that wraps the same foundation deploy + build-source upload (and seeds initial onboarded repos when the dashboard is off). It does not create OIDC providers, CI deploy roles, or per-agent runtime roles — that machinery has been retired.

### 3c. Store secrets in SSM

```bash
# Asana PAT (if using Asana triggers)
aws ssm put-parameter \
  --name /sdlc-agents/asana-pat \
  --value "$ASANA_PAT" \
  --type SecureString \
  --region $AWS_REGION

# Webhook secret is auto-populated by the Lambda on first Asana handshake
```

You do **not** create ECR repositories by hand — the shared build pipeline creates `sdlc-agents/<agent>` on the first build if it's missing.

---

## Step 4 — Add the agent code (only if you're adding a NEW agent)

The four agents above already ship in `agents/`. Skip to Step 5 if you're deploying one of them.

To add a brand-new agent, create `agents/<name>/` with the standard code shape:

```
agents/<name>/
  agent.py           # Strands agent entry point with @app.entrypoint
  prompts.py         # System prompt (versioned with code)
  tools/             # Custom @tool functions
  Dockerfile         # Container build (build context is agents/, so shared/ is available)
  requirements.txt
  tests/eval_dataset.json   # optional golden set for quality evaluation
```

Then re-upload the build source so the pipeline can build it (a redeploy that skips the foundation and dashboard is enough):

```bash
python scripts/deploy_fleet.py --stage $STAGE --region $AWS_REGION --skip-foundation --skip-dashboard
```

(A full `python scripts/deploy_fleet.py` run also re-uploads the source.) The `agent_id` you onboard in Step 6 must match the directory name under `agents/` in the source tree — an onboard for a nonexistent `agent_id` surfaces as a build failure.

---

## Step 5 — Add operators/admins to the dashboard

Onboarding happens in the dashboard's Admin view, which is gated by the Cognito `admins` group (operators who only view runs are in `operators`). There is no self sign-up — an admin creates users and adds them to the group. The dashboard URL and Cognito details are stack outputs (`DashboardUrl`, `DashboardUserPoolId`, …); `scripts/deploy_fleet.py` prints the published dashboard URL.

Add yourself (or the operator) to the `admins` group, then open the dashboard and sign in.

---

## Step 6 — Onboard each agent from the dashboard Admin view

In the dashboard **Admin view**, open the **Capabilities** panel and click **Onboard capability**. For each agent in `<AGENTS>`:

1. Enter the **`agent_id`** (must match the `agents/<name>/` directory in the uploaded build source).
2. Optionally set a **description**, **aliases** (comma-separated; the router lowercases mentions before matching), and **env** (`KEY=value, KEY2=value2`) for any agent-specific environment variables.
3. Click **Onboard**.

Onboarding writes the capability row and immediately:
1. Starts the shared build (`sdlc-agent-builder-${STAGE}` CodeBuild, with `AGENT_NAME=<agent_id>`) — builds `agents/<agent_id>` and pushes to ECR.
2. On build completion, an EventBridge event invokes the `capability-deployer` Lambda, which creates the per-agent runtime IAM role (under IAM path `/sdlc-agents/capabilities/`, capped by a permissions boundary) and the AgentCore runtime, waits for READY, then marks the capability **active**.
3. Marking it active re-renders the Dispatch Router registry from the capability rows and writes it to SSM (`/sdlc-agents/${STAGE}/registry`) — the agent is now routable.

The capability row shows its status (`building` → `active`, or `failed` with a reason). A failed build or a runtime that never reaches READY leaves any existing runtime untouched.

Editing a capability's fields (or re-onboarding) starts a fresh build — that's how you pick up new agent code after re-uploading the build source in Step 4.

---

## Step 7 — Configure Asana integration (if applicable)

**Ask the user:** "Do you want Asana triggers? This lets users assign tasks to agents or mention them in Asana comments."

If yes:

### 7a. Create bot accounts in Asana

For each agent being deployed with Asana triggers, create a dedicated Asana user account (e.g. `workitems-bot@yourorg.com`). These are the accounts users will "assign" tasks to in order to trigger agents.

Retrieve each bot's GID:
```bash
curl -s "https://app.asana.com/api/1.0/users/workitems-bot@yourorg.com" \
  -H "Authorization: Bearer $ASANA_PAT" | jq -r '.data.gid'
```

### 7b. Create the "Agent" custom field (for custom_field triggers)

In Asana, create an Enum custom field called **Agent** with values matching each deployed agent name (`workitems`, `docwriter`, `researcher`). Retrieve its GID from the workspace:

```bash
curl -s "https://app.asana.com/api/1.0/workspaces/$ASANA_WORKSPACE_GID/custom_fields" \
  -H "Authorization: Bearer $ASANA_PAT" | jq '.data[] | select(.name=="Agent") | .gid'
```

### 7c. Register the webhook

The Asana webhook URL is the API Gateway endpoint from the foundation stack (`WebhookEndpoint` output). Registration is handled by `scripts/bootstrap_asana_webhook.py`, which mediates the Asana handshake and stores the webhook secret in SSM (attaching a temporary `ssm:PutParameter` policy to the webhook Lambda's role only for the registration window). See the `sdlc-agents-register-triggers` skill for the details.

Verify in CloudWatch Logs for the `asana-webhook-${STAGE}` function.

---

## Step 8 — Configure GitHub @claude integration (optional)

**Ask the user:** "Do you want `@claude` to work in GitHub comments and PRs? This uses the `claude-code.yml` workflow."

If yes, `claude-code.yml` authenticates to Bedrock. The `sdlc-agents-setup-claude-code` skill provisions the `ClaudeCodeBedrockRole` (with `bedrock:InvokeModel` for `us.anthropic.claude-opus-4-7-v1`) and sets `CLAUDE_CODE_ROLE_ARN` + `CLAUDE_CODE_AWS_REGION` on the target repo. This is independent of the fleet — you can enable it or not.

---

## Verification checklist

Before reporting the setup as complete, confirm each of the following:

- [ ] `python scripts/deploy_fleet.py` completed without errors
- [ ] The dashboard is reachable and you can sign in as an admin
- [ ] The build source (`agents/`) was uploaded to the pipeline's source bucket
- [ ] Each chosen agent has an **active** capability row in the dashboard (not `building`/`failed`)
- [ ] AgentCore Runtimes for the active capabilities are in `READY` state
- [ ] SSM parameter `/sdlc-agents/${STAGE}/registry` is populated (re-rendered from the capability rows)
- [ ] If Asana: webhook registered and handshake logged in CloudWatch
- [ ] If Asana: bot GIDs stored in SSM / Lambda environment variables
- [ ] End-to-end: commenting `@workitems health check` on a GitHub issue gets a response
- [ ] `@claude` works on a test comment (if configured)

---

## Troubleshooting

**A capability is stuck in `building` or lands in `failed`**
Check the `sdlc-agent-builder-${STAGE}` CodeBuild run and the `capability-deployer-${STAGE}` Lambda's CloudWatch logs. Common causes: no `agents/<agent_id>/Dockerfile` in the uploaded source (re-run `deploy_fleet.py` to refresh `source.zip`), an invalid `agent_id`, or the runtime never reaching READY. A failed build/deploy leaves any existing runtime untouched.

**Dispatch Router returns 404 for a known agent**
The agent isn't an active capability. Confirm its capability row is `active` in the dashboard; the registry is re-rendered from active rows on every change and written to `/sdlc-agents/${STAGE}/registry`.

**Onboard fails with "a runtime named X already exists but is not managed by this fleet"**
An AgentCore runtime with that name exists without the fleet tag. The deployer refuses to overwrite a foreign runtime — choose a different `agent_id`.

**Asana webhook not triggering**
Check CloudWatch Logs for `asana-webhook-${STAGE}`. Common causes: webhook not registered, signature mismatch (SSM secret out of sync), or the Lambda's API Gateway URL changed after a stack update.

**`@claude` workflow fails with access denied on Bedrock**
The `ClaudeCodeBedrockRole` needs `bedrock:InvokeModel` for `us.anthropic.claude-opus-4-7-v1`. Re-run `sdlc-agents-setup-claude-code`.

---
name: sdlc-agents-provision-aws
description: Use when the user is ready to deploy the SDLC fleet's AWS base platform and onboard their selected agents. Deploys the foundation stack + shared build pipeline + dashboard (scripts/deploy_fleet.py), then onboards each agent from the dashboard Admin view (which builds its container and stands up its AgentCore runtime). Reads the agent selection from .sdlc-agents/selection.yaml. Idempotent. Invoked by sdlc-agents after the user confirms their agent list.
---

# Deploy the base platform and onboard the selected agents

The fleet uses **UI-driven onboarding**. You do NOT create per-agent IAM roles,
ECR repos, AgentCore runtimes, OIDC providers, CI deploy roles, or GitHub Actions
secrets/variables by hand — that machinery has been retired. There are two
phases:

1. **Deploy the base platform once** with `scripts/deploy_fleet.py` (foundation
   stack, shared build pipeline, build source, dashboard SPA).
2. **Onboard each selected agent** from the dashboard Admin view — onboarding
   builds the agent's container (shared `sdlc-agent-builder-<stage>` CodeBuild,
   parameterized by `AGENT_NAME`) and stands up its per-agent runtime IAM role +
   AgentCore runtime (the `capability-deployer` Lambda), then re-renders the
   Dispatch Router registry from the active capability rows.

## Prerequisites

- `.sdlc-agents/selection.yaml` exists and lists the agents, AWS account ID, region, and stage
- The user's current AWS credentials can reach the target account (`aws sts get-caller-identity` shows the right account) with enough privilege to run `sam deploy` (the base deploy uses the operator's own credentials — there is no CI deploy role)
- Bedrock model access is enabled for the Opus 4.7 cross-region inference profile in the target region (check: `aws bedrock get-inference-profile --inference-profile-identifier us.anthropic.claude-opus-4-7 --region $REGION`). If the call 404s, stop and tell the user to request access in the Bedrock console before continuing.

## Step 1 — Deploy the base platform

`scripts/deploy_fleet.py` is the one-command base-platform deployer. Run it from
the installer repo (not `$TARGET_REPO`). You need the dashboard enabled to
onboard agents, so turn it on (and the AgentCore Gateway, if the user wants the
enforced tool-call boundary).

```bash
STAGE=$(yq '.aws.stage // "dev"' "$TARGET_REPO/.sdlc-agents/selection.yaml")
REGION=$(yq '.aws.region' "$TARGET_REPO/.sdlc-agents/selection.yaml")

# Preview first (touches nothing):
python scripts/deploy_fleet.py --stage "$STAGE" --region "$REGION" --dry-run
```

The script runs `sam build`/`sam deploy` on `infra/foundation`, uploads the
`agents/` tree as the build source, and builds/publishes the dashboard SPA. It's
idempotent and **preserves the stack's existing parameter values on re-run** — so
if the dashboard/gateway aren't on yet, set them explicitly the first time with
your own `sam deploy` (or a `--parameter-overrides` pass), e.g.
`DeployDashboard=true` and `DeployGateway=true`. See `docs/aws-deploy.md` §2.3
for the full parameter list.

By default the foundation changeset is printed and must be confirmed before it
applies IAM/networking changes; pass `--auto-approve` only for unattended runs.

## Step 2 — Add dashboard operators/admins

Onboarding lives in the dashboard's Admin view, gated by the Cognito `admins`
group (view-only operators go in `operators`). There's no self sign-up — create
the users and add them to the group. The dashboard URL and Cognito pool are stack
outputs (`DashboardUrl`, `DashboardUserPoolId`), also printed by the deploy
script. Add the operator to `admins` and confirm they can sign in.

## Step 3 — Onboard each selected agent

For each agent in `.sdlc-agents/selection.yaml`, onboard it from the dashboard
**Admin view → Capabilities** panel (**Onboard capability**):

- **`agent_id`** — must match a directory under `agents/` in the uploaded build
  source (e.g. `workitems`). An `agent_id` with no matching `agents/<id>/`
  surfaces as a build failure, and the capability lands in `failed`.
- **description / aliases / env** — optional. Aliases are the `@mention` names
  (the router lowercases before matching). `env` (`KEY=value, KEY2=value2`)
  carries the agent's per-agent environment — this is where values like
  `GITHUB_REPO`, `ASANA_WORKSPACE_GID`, `ASANA_PROJECT_GID`, and
  `ASANA_PROJECT_NAME` go. Reserved keys (guardrail id/version, `GATEWAY_MCP_URL`)
  are supplied by the deployer's base env and rejected if you try to set them.

Onboarding writes the capability row and kicks off the build. The row moves
`building` → `active` on its own as the build finishes and the
`capability-deployer` Lambda stands up the runtime and waits READY. If it lands
in `failed`, read the reason on the row and check the `sdlc-agent-builder-${STAGE}`
CodeBuild run + `capability-deployer-${STAGE}` Lambda logs.

If `sdlc-agents-select` proposed an agent that has no `agents/<id>/` directory in
this repo, stop and tell the user — it means the agent is planned but not shipping
and shouldn't have been selected.

There is no separate registry step: marking a capability active re-renders the
Dispatch Router registry from the active rows and writes it to
`/sdlc-agents/${STAGE}/registry`.

## Step 4 — Onboard the target repos the fleet may act in

GitHub mentions from a non-onboarded repo are rejected at dispatch. In the Admin
view's repo panel, onboard the user's target repo(s) (enabled + eligible). When
the dashboard is off, `scripts/bootstrap.py` seeds initial repos into
`fleet-config-${STAGE}` instead; you can also write repo rows to that table
directly (`pk="repo#<owner/repo>"`, lowercase).

## AWS org gotchas to flag

AgentCore is new and AWS org-level SCPs sometimes block `bedrock-agentcore:*`
actions even with broad permissions. If the `capability-deployer` Lambda fails to
create/update a runtime with `AccessDeniedException`:

- Check the deployer's CloudWatch logs for the exact denied action.
- Simulate the policy on the deployer's role: `aws iam simulate-principal-policy --policy-source-arn <capability-deployer role> --action-names bedrock-agentcore:CreateAgentRuntime`
- If simulate shows allowed but the API returns denied, it's an SCP. Tell the user — you can't fix org SCPs from here.

## When you're done

Output a summary:

```
Base platform deployed (stage=<STAGE>, region=<REGION>): foundation stack,
shared build pipeline, dashboard at <DashboardUrl>.

Onboarded capabilities (from the dashboard Admin view):
  workitems:  active
  researcher: active
  docwriter:  active

Repos onboarded: <owner>/<repo>

Next: connect integrations. Run the matching connect skills for your tools:
  sdlc-agents-connect-asana (your PM)
  sdlc-agents-connect-github (your SCM)
```

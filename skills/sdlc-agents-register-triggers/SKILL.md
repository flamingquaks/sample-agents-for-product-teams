---
name: sdlc-agents-register-triggers
description: Use when the user's agents are provisioned and integrations are connected, and they need to enable the event triggers that make agents actually respond to @mentions. Registers Asana webhooks, confirms the GitHub App webhook subscription, configures Slack app event subscriptions, and updates the dispatch router Lambda environment for the selected agents. Invoked by sdlc-agents after the connect skills finish.
---

# Wire up event triggers so agents respond to mentions

## Prerequisites

- `.sdlc-agents/selection.yaml` has the agent list, toolchain, and GIDs captured by the connect skills
- The shared foundation stack is deployed (`dispatch-router-${STAGE}` Lambda + `asana-webhook-${STAGE}` Lambda + API Gateway exist)
- Each selected agent has been onboarded from the dashboard Admin view and its capability row is **active** — onboarding builds the container and stands up the AgentCore Runtime (`capability-deployer` Lambda), then re-renders the Dispatch Router registry from the active capability rows, so at this point the SSM registry (`/sdlc-agents/${STAGE}/registry`) is current. If a capability is still `building` or `failed`, finish onboarding it (check the `sdlc-agent-builder-${STAGE}` CodeBuild + `capability-deployer-${STAGE}` logs) before proceeding — there's no manual registry-sync step.

## Per-integration wiring

### Asana

#### 1. Update the webhook Lambda's environment

The Lambda reads the "Agent" custom field GID and bot user GIDs from its environment. Swap them to the customer's values:

```bash
aws lambda update-function-configuration \
  --function-name "asana-webhook-${STAGE}" \
  --environment "Variables={
    ASANA_WEBHOOK_SECRET_PARAM=/sdlc-agents/asana-webhook-secret,
    ASANA_PAT_PARAM=/sdlc-agents/asana-pat,
    DISPATCH_FUNCTION=dispatch-router-${STAGE},
    AGENT_FIELD_GID=<from selection.yaml asana.agent_field_gid>,
    WORKITEMS_BOT_GID=<asana user gid the Workitems agent should act as>
    }" \
  --region "$REGION"
```

If the user has agents beyond workitems (`docwriter`, `researcher`) and wants assignment-based triggers for them, add `DOCWRITER_BOT_GID` and `RESEARCHER_BOT_GID` as separate bot users. For dev/demo, the user's own Asana GID is fine for all of them.

#### 2. Redeploy the Lambda code if this is a fresh install

If the webhook Lambda was deployed before your agent list or alias map changed, rebuild its zip and push:

```bash
cd infra/dispatch
pip install --quiet --target /tmp/lambda-build -r requirements.txt
cp asana_webhook.py router.py /tmp/lambda-build/
(cd /tmp/lambda-build && zip -rq /tmp/asana-webhook.zip . -x '*.pyc' -x '__pycache__/*')
aws lambda update-function-code \
  --function-name "asana-webhook-${STAGE}" \
  --zip-file fileb:///tmp/asana-webhook.zip \
  --region "$REGION"
```

Wait for `LastUpdateStatus=Successful` before proceeding.

#### 3. Register the Asana webhook

Registration is handled by `scripts/bootstrap_asana_webhook.py`. The script mediates the Asana handshake: in steady state the webhook Lambda's IAM role does **not** hold `ssm:PutParameter` on the webhook-secret parameter (threat T-9 — a Lambda that can overwrite the secret is a foothold for an attacker who can replay the handshake). The script attaches a temporary inline policy for the registration window, calls the Asana webhooks API, waits for the handshake to populate the secret in SSM, and removes the inline policy.

Choose a scope:

- **Workspace-scoped** (all tasks across all projects): pass the workspace GID. Filters must match Asana's whitelist — `story.added` and `task.changed[assignee, custom_fields]` are known to work.
- **Project-scoped** (one specific project): pass the project GID. Recommended for demos. If the user has multiple projects, register one webhook per project.

Run the script with operator credentials (needs `iam:PutRolePolicy` / `iam:DeleteRolePolicy` on the Lambda's execution role):

```bash
python scripts/bootstrap_asana_webhook.py \
  --stage "$STAGE" \
  --region "$REGION" \
  --resource-gid <project_or_workspace_gid>
```

The script prints the Asana webhook GID on success. If the handshake times out, inspect the Lambda's CloudWatch logs — the most common cause is IAM propagation lag, and a retry usually succeeds. Do NOT re-run the Asana API call by hand with a long-lived elevated Lambda role; that is exactly the posture T-9 closes off.

### GitHub

The GitHub `@mention` trigger is the fleet's **GitHub App webhook**, not a
GitHub Actions workflow. There is no per-repo `agent-dispatch.yml` to enable and
no OIDC deploy role to scope — those were retired. Wiring GitHub triggers is
therefore about the App, not the repo:

#### 1. Confirm the App webhook is delivering to the fleet endpoint

The GitHub App (registered in `sdlc-agents-connect-github`) has its webhook URL
pointed at the fleet's `github-webhook-${STAGE}` endpoint (the `WebhookEndpoint`-style
API Gateway route for GitHub) and its webhook secret stored in SSM
(`/sdlc-agents/github-webhook-secret`). The App is subscribed to `Issue comment`
and `Pull request review comment` events. One App webhook serves **every** repo
the App is installed on — there is no per-repo enablement step.

#### 2. Confirm the repos are onboarded and the App is installed

For each repo the user wants agents to act in, confirm it is onboarded in the
dashboard Admin view (which verifies the App is installed on the owner and can
reach the repo). Mentions from a non-onboarded repo are rejected at dispatch.
Agent mention tokens are resolved by the webhook Lambda against the live registry;
there is no hardcoded `if:` trigger list to edit.

### Slack

Slack is a shipped trigger source. The `slack-webhook-${STAGE}` Lambda is **always deployed** (no `DeploySlack` flag — it's serverless, inert at rest, and fails closed at runtime), serving three routes on the webhook API: `/slack/events` (Events API `app_mention`), `/slack/commands` (`/fleet @agent …`, `/sdlc-onboard-channel`, `/sdlc-notify`), and `/slack/interactions` (the `/sdlc-notify` Block Kit modal submit). The receiver verifies the Slack `v0` signature (±5-min replay window), dedups on `event_id`, guards bot-loops, and resolves `@mentions` against the live registry exactly like the other sources. Slack goes live only when an admin onboards a workspace.

#### 1. Register the Slack app + store secrets

The receiver is already deployed; note the `SlackEventsEndpoint`, `SlackCommandsEndpoint`, and `SlackInteractionsEndpoint` stack outputs. Run `scripts/bootstrap_slack.py` (operator credentials):

- `manifest --webhook-base <api-base>` prints the Slack app manifest (scopes, event subscription, slash commands, **interactivity → `/slack/interactions`**) to paste into api.slack.com → Create from manifest. Install the app, then copy its Signing Secret + Bot Token.
- `store --stage … --team-id … --signing-secret … --bot-token …` writes the secrets to SSM SecureString:
  - **App signing secret** at `/sdlc-agents/${STAGE}/slack/signing-secret` — **app-level** (one per Slack app; the `url_verification` handshake carries no team scope, so verification can't depend on a `team_id`).
  - **Bot token** (`xoxb-…`) at `/sdlc-agents/${STAGE}/slack/<team_id>/bot-token` — **per-workspace/installation**; run once per workspace you onboard.

Scopes: `app_mentions:read`, `chat:write`, `commands`, `users:read`, `users:read.email` (the last backs email-based identity resolution — see the Users & Groups section).

#### 2. Onboard each workspace + channel

In the dashboard **Connectors → Slack** panel, onboard the workspace (an enabled, active `slack_workspace` row — deliveries from any other `team_id` are rejected). Channel access is default-deny under the recommended allowlist posture: users request a channel with `/sdlc-onboard-channel [agent …]` and an **admin approves** it in the same panel (never self-served). No dispatch is authorized until a trigger-authz grant exists — see the next section.

#### 3. (Optional) Notifications

Once a workspace is live, users self-serve channel notifications with `/sdlc-notify` — an interactive modal with three tiers (actionable / informative / error) and a repo scope bounded to onboarded repos. Admins can review/remove subscriptions in **Connectors → Slack → Notifications**. Subscriptions only *receive* (they grant no access), so they need no approval.

## Trigger authorization (required — no dispatch runs without it)

Wiring a receiver only gets an event to the Router. The Router then **authorizes every dispatch** against the AVP `TriggerPolicyStore` (`infra/dispatch/trigger_authz.py`), which is the fleet's **sole** trigger-authz mechanism. The old per-capability `authorization.users` allowlist has been **removed** — do not look for it. The store is **default-deny**: an onboarded agent is not triggerable by anyone until an admin authors a grant.

Grants are **data, not policy** — authored in the dashboard **Connectors → Trigger Rules** panel (each grant is a `trigger_rule` DynamoDB row), so granting a user is a data write and the AVP policy count stays fixed. A rule is: subject (a `user` principal id, or a `group`) → agent (`*` = any) → workspace (`*` = any), with an effect of permit or forbid (forbid wins).

Principals are **immutable, source-namespaced** ids (never a display name):

- GitHub → `github:<login>`
- Asana → `asana:<user_gid>`
- Slack → `slack:<team_id>:<user_id>` (a channel-scoped grant `channel:<team>:<channel>` permits anyone triggering from an approved channel)

To let one agent trigger another (cross-agent chains), grant the peer agent's bot identity a permit on the callee. Confirm at least one permit grant exists for each agent the user expects to be triggerable before running verify, or every mention will be denied (with a `TriggerDenied` metric + an in-thread reject notice).

## Verify the pipeline

Run `sdlc-agents-verify` to smoke-test each enabled trigger path.

## What this skill does NOT do

- Create Asana bot accounts (those are users, not API resources — user has to invite them manually)
- Configure Asana's "Agent" custom field enum options (that was done in `sdlc-agents-connect-asana`)
- Wire up webhooks for tools the user doesn't have (skip non-selected integrations cleanly)

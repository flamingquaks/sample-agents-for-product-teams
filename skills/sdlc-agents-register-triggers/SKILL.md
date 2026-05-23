---
name: sdlc-agents-register-triggers
description: Use when the user's agents are provisioned and integrations are connected, and they need to enable the event triggers that make agents actually respond to @mentions. Registers Asana webhooks, enables the GitHub Actions dispatch workflow, configures Slack app event subscriptions, and updates the dispatch router Lambda environment for the selected agents. Invoked by sdlc-agents after the connect skills finish.
---

# Wire up event triggers so agents respond to mentions

## Prerequisites

- `.sdlc-agents/selection.yaml` has the agent list, toolchain, and GIDs captured by the connect skills
- The shared foundation stack is deployed (`dispatch-router-${STAGE}` Lambda + `asana-webhook-${STAGE}` Lambda + API Gateway exist)
- The first successful CI deploy of each selected agent has completed — `deploy-agent.yml` creates the AgentCore Runtime and then runs `scripts/sync_registry.py` automatically, so at this point `.dispatch/agents.yaml` has the runtime ARNs and the SSM registry is current. If you're re-running this skill after changing the agent selection, re-run `python scripts/sync_registry.py --stage $STAGE --region $REGION` manually before proceeding.

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

#### 1. Enable the agent-dispatch workflow

`.github/workflows/agent-dispatch.yml` already exists in the repo. Ensure its trigger list covers the user's selected agents — it's a hardcoded `if:` block:

```yaml
if: |
  contains(github.event.comment.body, '@workitems') ||
  contains(github.event.comment.body, '@docwriter') ||
  ...
```

Add a line for each agent in `.sdlc-agents/selection.yaml`. Commit.

#### 2. Confirm the deploy role OIDC trust is scoped to the target repo

The trust policy for the deploy role is created manually (or via `sdlc-agents-provision-aws`) when you first set up the account — see `docs/aws-deploy.md` §1.3. It should use `StringEquals` on `sub` with two explicit subjects: `repo:<ORG>/<REPO>:ref:refs/heads/main` (covers deploy workflows on `push` to main AND the dispatch workflow's `issue_comment` / `pull_request_review_comment` events, which run in the default-branch context) and `repo:<ORG>/<REPO>:pull_request` (covers `claude-code.yml`'s `pull_request: [opened, synchronize]` trigger — the only true "pull request event" in OIDC terms). If the user is bringing a brand-new repo, the role's trust policy was scoped to a different `<ORG>/<REPO>` — update the role directly (`aws iam update-assume-role-policy`) to add their repo's two subjects, or create a fresh role for them.

### Slack

#### 1. Update the webhook Lambda's environment (if needed)

The Slack webhook Lambda reads its token and signing-secret SSM paths from environment variables. In most cases the defaults baked into the SAM template are correct (`/sdlc-agents/slack-bot-token`, `/sdlc-agents/slack-signing-secret`). If the user stored their credentials at different paths, update the Lambda:

```bash
aws lambda update-function-configuration \
  --function-name "slack-webhook-${STAGE}" \
  --environment "Variables={
    SLACK_BOT_TOKEN_PARAM=/sdlc-agents/slack-bot-token,
    SLACK_SIGNING_SECRET_PARAM=/sdlc-agents/slack-signing-secret,
    DISPATCH_FUNCTION=dispatch-router-${STAGE}
    }" \
  --region "$REGION"
```

#### 2. Redeploy the Lambda code if this is a fresh install

If the foundation stack was deployed before the Slack Lambda code existed, push the latest zip:

```bash
cd infra/dispatch
pip install --quiet --target /tmp/lambda-build -r requirements.txt
cp slack_webhook.py router.py reply.py /tmp/lambda-build/
(cd /tmp/lambda-build && zip -rq /tmp/slack-webhook.zip . -x '*.pyc' -x '__pycache__/*')
aws lambda update-function-code \
  --function-name "slack-webhook-${STAGE}" \
  --zip-file fileb:///tmp/slack-webhook.zip \
  --region "$REGION"
```

Wait for `LastUpdateStatus=Successful` before proceeding.

#### 3. Run bootstrap_slack_app.py

`scripts/bootstrap_slack_app.py` generates the Slack app manifest, walks through app creation, prompts for the bot token and signing secret, and stores both in SSM. It then prints the endpoint URLs to paste into the app config.

Unlike the Asana bootstrap, there is no handshake protocol — the signing secret is a static value from the Slack app's Basic Information page. The Lambda has no `ssm:PutParameter` in steady state (no T-9 analogue exists for Slack, but the defensive posture is the same: write access is not granted unless actually needed).

```bash
python scripts/bootstrap_slack_app.py \
  --stage "$STAGE" \
  --region "$REGION"
```

The script reads `.dispatch/agents.yaml` to determine which agents get slash commands in the manifest. Run it from the fleet repository root.

#### 4. Paste endpoint URLs into the app config

The script prints two URLs at the end (also available as CloudFormation stack outputs):

- **SlackEventsEndpoint** → paste into the app's **Event Subscriptions → Request URL** field, then save. Slack sends a `url_verification` challenge; the Lambda echoes it back automatically.
- **SlackCommandsEndpoint** → paste into each slash command's **Request URL** field (under **Slash Commands** in the app config).

If the manifest used placeholder URLs (stack wasn't deployed when you ran the script), update the app config manually via https://api.slack.com/apps.

#### 5. Re-install the app to the workspace if scopes changed

Any time you add new OAuth scopes (e.g. adding `commands` after the initial install), click **Install to Workspace** again on the app's **Install App** page. Slack requires a fresh install to activate scope changes.

To verify the integration is working, mention the bot in a channel it belongs to:

```
@SDLC Agents @workitems what are the open high-priority items?
```

Check CloudWatch logs for `slack-webhook-${STAGE}` — you should see `Dispatching to workitems (mention) from Slack` followed by a Lambda invoke entry.

## Verify the pipeline

Run `sdlc-agents-verify` to smoke-test each enabled trigger path.

## What this skill does NOT do

- Create Asana bot accounts (those are users, not API resources — user has to invite them manually)
- Configure Asana's "Agent" custom field enum options (that was done in `sdlc-agents-connect-asana`)
- Wire up webhooks for tools the user doesn't have (skip non-selected integrations cleanly)

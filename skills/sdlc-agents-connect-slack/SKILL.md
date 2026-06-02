---
name: sdlc-agents-connect-slack
description: Use when the user needs to connect SDLC agents to Slack. Walks through creating the Slack app from the bundled manifest, capturing the signing secret and bot token, storing both in SSM, configuring event subscriptions and slash commands, and verifying the receiver. Knows the Slack-specific pitfalls (3-second ack requirement, signature verification, bot-loop prevention, SecureString storage).
---

# Connect the SDLC Agent Fleet to Slack

## Two access channels

Like GitHub and Asana, Slack access splits into an inbound and an outbound side — but for Slack both run over the **direct Slack Web API**, not an MCP server:

- **Inbound: Slack → `slack-events-${STAGE}` Lambda.** Slack posts events and slash commands to an API Gateway endpoint. The Lambda verifies the request signature (HMAC-SHA256 over `v0:{timestamp}:{body}`) using the **signing secret**, then forwards a normalized event to the Dispatch Router.
- **Outbound: agents / dispatch → Slack Web API.** Agents post results, threaded replies, and reactions using the **bot token** (`chat.postMessage`, `reactions.add`). This is the `agents/shared/tools/slack_post.py` tool path — no Slack MCP server is involved.

Both credentials live in SSM as **SecureString**. Nothing Slack-related is passed as a CloudFormation parameter, so no secret ever lands in `samconfig.toml` or the stack template.

## Prerequisites

- The foundation stack is deployed (`slack-events-${STAGE}` Lambda + API Gateway exist). Its IAM role already grants `ssm:GetParameter` on `/sdlc-agents/slack-signing-secret` and `/sdlc-agents/slack-bot-token` — but those parameters don't exist until you create them in this skill.
- `$REGION` and `$STAGE` are set (recorded at `.sdlc-agents/selection.yaml` → `aws.region` / `aws.stage`).
- Admin access to the target Slack workspace. **Use a test/sandbox workspace, not a production one**, until the integration is proven.

## The bundled bootstrap script does most of this

`scripts/bootstrap_slack_app.py` automates the create-app → install → capture-secrets → store-in-SSM flow and auto-discovers the API Gateway URL from the foundation stack. Prefer it over hand-running each step:

```bash
python scripts/bootstrap_slack_app.py --region "$REGION" --stage "$STAGE"
```

It will print the Events URL and Slash URL, walk the user through pasting the manifest at api.slack.com/apps, then prompt for the Bot Token (`xoxb-...`) and Signing Secret and store both as SecureString. The sections below document what it does so you can verify each step or do it by hand if the script can't run.

## Step 1 — Get the API Gateway endpoints

The Slack app's request URLs point at the foundation stack's API Gateway. Read them from the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name "sdlc-agents-foundation-${STAGE}" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'Slack')].[OutputKey,OutputValue]" \
  --output text
```

You want `SlackEventsEndpoint` (`.../slack/events`) and `SlackSlashEndpoint` (`.../slack/slash`). If they're absent, the foundation stack predates the Slack resources — redeploy it from this branch before continuing.

## Step 2 — Create the Slack app from the manifest

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest** → select the workspace.
2. Paste the contents of `infra/slack-app-manifest.yaml`.
3. Replace `<EVENTS_URL>` with the `SlackEventsEndpoint` value and `<SLASH_URL>` with the `SlackSlashEndpoint` value.
4. Create the app.

The manifest already declares the bot scopes (`app_mentions:read`, `assistant:write`, `chat:write`, `chat:write.public`, `commands`, `channels:history`, `channels:read`, `im:history`, `im:read`, `im:write`, `reactions:read`, `reactions:write`, `users:read`), the event subscriptions (`app_mention`, `message.im`, `assistant_thread_started`, `app_home_opened`), and the five slash commands (`/workitems`, `/researcher`, `/docwriter`, `/adr`, `/fleet`). Socket Mode is intentionally off — the fleet uses the Events API via API Gateway.

## Step 3 — Install to the workspace and capture credentials

1. In the app's **OAuth & Permissions** page, click **Install to Workspace** and authorize.
2. Copy the **Bot User OAuth Token** (`xoxb-...`).
3. From **Basic Information → App Credentials**, copy the **Signing Secret**.

Store both in SSM as SecureString (the bootstrap script does this for you; this is the manual form):

```bash
read -rs SLACK_BOT_TOKEN
aws ssm put-parameter \
  --name /sdlc-agents/slack-bot-token \
  --value "$SLACK_BOT_TOKEN" \
  --type SecureString \
  --region "$REGION" \
  --overwrite
unset SLACK_BOT_TOKEN

read -rs SLACK_SIGNING_SECRET
aws ssm put-parameter \
  --name /sdlc-agents/slack-signing-secret \
  --value "$SLACK_SIGNING_SECRET" \
  --type SecureString \
  --region "$REGION" \
  --overwrite
unset SLACK_SIGNING_SECRET
```

The parameter **names** must match exactly — they're the values the `slack-events-${STAGE}` Lambda and the agent runtimes read (`SLACK_SIGNING_SECRET_PARAM`, `SLACK_BOT_TOKEN_PARAM`). For production, store these under a customer-managed KMS key rather than the default `alias/aws/ssm`, and tighten the key policy to the Lambda/agent execution roles.

## Step 4 — Let Slack verify the Events URL

Back in the Slack app's **Event Subscriptions** page, the Request URL must show **Verified**. The `slack-events` Lambda answers Slack's `url_verification` challenge automatically — if it shows a red error:

- The signing secret in SSM doesn't match the app's actual secret (re-check Step 3).
- The foundation stack isn't deployed, or the URL is wrong (re-check Step 1).
- Inspect `/aws/lambda/slack-events-${STAGE}` in CloudWatch — a 401 means signature mismatch, a 503 means the Lambda couldn't read the signing secret from SSM.

## Step 5 — Authorize Slack users

The Dispatch Router fails closed: an empty `authorization.users` list rejects every sender. Slack identities are **user IDs** (e.g. `U0123ABCDEF`), which are stable and non-editable — never display names. The user finds theirs via Profile → ⋯ → **Copy member ID**.

This is recorded in the registry by `sdlc-agents-register-triggers` (it edits `.dispatch/agents.yaml` and runs `sync_registry.py`). Just capture the IDs here and note them in `selection.yaml`.

## Verify

```bash
# Confirms both Slack secrets exist in SSM and the bot token is live.
python3 <<'PY'
import os, boto3, requests
region = os.environ["REGION"]
ssm = boto3.client("ssm", region_name=region)
for name in ("/sdlc-agents/slack-signing-secret", "/sdlc-agents/slack-bot-token"):
    try:
        ssm.get_parameter(Name=name, WithDecryption=True)
        print(f"OK   {name} present")
    except Exception as e:
        print(f"MISS {name}: {e}")
try:
    tok = ssm.get_parameter(Name="/sdlc-agents/slack-bot-token", WithDecryption=True)["Parameter"]["Value"]
    r = requests.post("https://slack.com/api/auth.test",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()
    print(f"auth.test: ok={r.get('ok')} team={r.get('team')} bot={r.get('user')}" if r.get("ok")
          else f"auth.test failed: {r.get('error')}")
except Exception as e:
    print(f"bot token check skipped: {e}")
PY
```

`auth.test` returning `ok=True` with the workspace name confirms the bot token is valid and installed. A full end-to-end trigger test (mention → reply) is run by `sdlc-agents-verify` once triggers are registered.

## Record Slack state

Append to `$TARGET_REPO/.sdlc-agents/selection.yaml`:

```yaml
slack:
  app_installed: true
  team_id: <TXXXXXXXX>        # the workspace ID from auth.test
  authorized_users:           # Slack user IDs allowed to invoke agents
    - U0123ABCDEF
```

(`authorized_users` is consumed by `sdlc-agents-register-triggers` when it populates `authorization.users` in `.dispatch/agents.yaml`.)

## What this skill does NOT do

- Populate `authorization.users` in `.dispatch/agents.yaml` or sync the registry — that's `sdlc-agents-register-triggers`.
- Deploy the `slack-events-${STAGE}` Lambda — that's part of the foundation stack (`sdlc-agents-provision-aws`).
- Run the end-to-end mention smoke test — that's `sdlc-agents-verify`.
- Set up Microsoft Teams or other chat platforms. Only Slack has a connect skill; if the user is on Teams, flag it as a gap (no receiver exists).

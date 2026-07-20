---
name: sdlc-agents-connect-github
description: Use when the user needs to connect SDLC agents to GitHub. Walks through registering the fleet's GitHub App (the only credential model — the shared PAT was retired), installing it per owner, onboarding repos, and verifying the agent + webhook-trigger paths. Knows the specific GitHub pitfalls (fine-grained App permissions, org SSO enforcement).
---

# Connect the SDLC Agent Fleet to GitHub

## Two access channels

Like Asana, GitHub access splits:

- **Agent runtime → GitHub** — **gateway-only**: agents SigV4-invoke the AgentCore Gateway, which routes GitHub through its SCM broker target. Agents hold no GitHub credential; the broker mints a per-owner **GitHub App** installation token per call, scoped to the co-approved repo set + the agent's permission tier. **This is the only credential model**: the shared PAT and its `GITHUB_AUTH_MODE` selector have been retired, and there is no direct-to-GitHub path (the gateway is the required policy + observability chokepoint). Prerequisite: deploy the foundation stack with `DeployGateway=true` (and `DeployDashboard=true`).
- **GitHub → fleet (mention triggers)** — the same GitHub App's **webhook** delivers `@mention` events to the fleet's `github-webhook-${STAGE}` endpoint, HMAC-verified by `infra/dispatch/github_webhook.py`. No GitHub Actions workflow and no OIDC deploy role are involved — both were retired. Nothing is stored in the repo's GitHub Actions settings for the fleet.

## GitHub App (the only path)

The App path is driven from the **dashboard admin UI**, not hand-run `put-parameter`
commands — the UI's manifest flow registers the App and stores its credentials for
you, and onboarding then verifies a per-owner installation. Prerequisite: deploy the
foundation stack (the App credential resources are created unconditionally, but
the manifest flow that populates them lives in the dashboard, so registering the
App needs `DeployDashboard=true` at least once).

1. In the dashboard **Admin → GitHub App** panel, click **Set up GitHub App**
   (optionally enter an org to install org-wide vs. on your user account). This
   POSTs a GitHub App *manifest* to GitHub; you confirm the App's permissions
   there and are redirected back.
   - The manifest requests: Contents R&W, Issues R&W, Pull requests R&W,
     Metadata Read (see `github_client.APP_PERMISSIONS`). **Webhook enabled** —
     the App's webhook (URL = the fleet's `github-webhook-${STAGE}` endpoint,
     secret in SSM) is what listens for `@agent` mentions, subscribed to
     `Issue comment` + `Pull request review comment`. One App webhook serves
     every onboarded repo (this replaced the retired `agent-dispatch.yml`).
2. On return, the admin API's manifest exchange persists the credentials
   automatically: the **private key → Secrets Manager**
   (`sdlc-agents/github-app/private-key`), and the **app id + slug → SSM String**
   (`/sdlc-agents/github-app-id`, `…-slug`). You do **not** store these by hand.
3. Click **Install on GitHub** and install the App on each user/org whose repos
   you'll onboard. Installation IDs are **not** a single SSM param — they are
   resolved per owner at onboard time and recorded per owner in DynamoDB, so one
   App spans many individual and org owners.
4. Onboard a repo (Admin → **Onboard repository**). The API always verifies the
   App is installed on the repo's owner and can reach the repo *before*
   activating it; if it isn't installed it returns an install deep-link + Re-check.
   - **Multi-repo eligible** — whether the repo may participate in cross-repo
     actions at all.
   - **Approved to run with** (`co_repo_mode`) — for a dispatch that originates in
     this repo, which OTHER repos it may act on: *only itself* (isolated),
     *a named group* (mutual — repos sharing a group name may operate on each
     other, and a group can span personal + org owners), or *all* eligible repos.
     The minted GitHub App token is scoped to exactly this set, so a dispatch
     physically cannot touch a repo it isn't approved to run with. Each agent also
     gets only its GitHub permission tier (docwriter opens PRs + pushes code;
     workitems manages issues but can't write code; adr reads code + comments only;
     researcher has no GitHub) — see `fleet_policy.AGENT_GITHUB_PERMISSIONS`.

> **State:** complete end-to-end, PAT-free, and gateway-only. Onboarding/
> verification (`infra/dashboard/github_client.py`), the **SCM broker + REQUEST
> interceptor** the gateway routes GitHub through (`infra/dispatch/scm_broker.py`,
> `infra/dispatch/scm_interceptor.py`), and the dispatch **reply Lambda**
> (`infra/dispatch/github_app.py`) all mint per-owner GitHub App installation
> tokens, scoped per call to the co-approved repo set + the agent's permission
> tier. Agents hold no GitHub credential and route ALL tool calls through the
> gateway. No shared PAT, no `GITHUB_AUTH_MODE` flag, no direct-to-GitHub path.
> See `docs/specs/github-onboarding-spec.md`.

## Verify

Registration + install status is visible in the **Admin → GitHub App** panel
(`configured`, App slug, install deep-link). To confirm the stored App can mint a
token from the CLI:

```bash
# Reads $REGION and $STAGE from the environment (see .sdlc-agents/selection.yaml).
python3 <<'PY'
import os, time, json, base64, boto3, requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

region, stage = os.environ["REGION"], os.environ.get("STAGE", "dev")
ssm = boto3.client("ssm", region_name=region)
sm = boto3.client("secretsmanager", region_name=region)
app_id = ssm.get_parameter(Name=f"/sdlc-agents/{stage}/github-app-id")["Parameter"]["Value"]
if app_id in ("", "unset"):
    print("GitHub App not registered yet — use the dashboard Admin → GitHub App panel."); raise SystemExit
pem = sm.get_secret_value(SecretId=f"sdlc-agents/{stage}/github-app/private-key")["SecretString"]

def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
now = int(time.time())
hdr, pl = {"alg":"RS256","typ":"JWT"}, {"iat":now-60,"exp":now+540,"iss":app_id}
si = f"{b64(json.dumps(hdr).encode())}.{b64(json.dumps(pl).encode())}".encode()
key = serialization.load_pem_private_key(pem.encode(), password=None)
jwt = f"{si.decode()}.{b64(key.sign(si, padding.PKCS1v15(), hashes.SHA256()))}"
r = requests.get("https://api.github.com/app/installations",
                 headers={"Authorization": f"Bearer {jwt}", "Accept":"application/vnd.github+json"}, timeout=15)
print(f"App JWT accepted: {r.status_code}; installations: {[i['account']['login'] for i in r.json()] if r.status_code==200 else r.text[:200]}")
PY
```

A 200 with the list of owners the App is installed on confirms the key + app-id
are valid and the App can mint per-owner tokens. A 401 means the stored key/app-id
don't match — re-run the manifest flow in the dashboard.

## No CI credentials to configure

The fleet needs **no** GitHub Actions secrets or OIDC role: `@mention` triggers
arrive via the App webhook (above), and build/deploy is server-side (CodeBuild +
capability-deployer). The old deploy-role OIDC trust and `AWS_DEPLOY_ROLE_ARN`
secret have been retired. (The only exception is the optional Claude Code on
Bedrock feature — a separate skill, `sdlc-agents-setup-claude-code` — which
installs its own workflow + OIDC role into the target repo. That is not part of
connecting the fleet to GitHub.)

## Record GitHub state

Append to `.sdlc-agents/selection.yaml`:

```yaml
github:
  auth: github-app      # the only model — per-owner installation tokens
  owner: <owner>        # the user's GitHub org or username
  repo: <repo>          # the target repository name
  default_branch: main
```

(`owner/repo/default_branch` are used by `docwriter` and `adr`, and by any future agent that opens PRs.)

## What this skill does NOT do

- Confirm the App webhook subscription / onboard the repos for triggering. That's `sdlc-agents-register-triggers`.
- Install Claude Code Action for `@claude` in issues. That's a separate Anthropic-provided action, not part of this fleet.
- Handle GitLab. If the user is on GitLab, use `sdlc-agents-connect-gitlab` (not yet written — flag it as a gap if asked).

---
name: sdlc-agents-connect-github
description: Use when the user needs to connect SDLC agents to GitHub. Walks through installing GitHub's official remote MCP server access, capturing credentials, configuring the deploy-role OIDC trust, and verifying agent + CI paths. Knows the specific GitHub pitfalls (GitHub App vs PAT, fine-grained vs classic PAT, org SSO enforcement).
---

# Connect the SDLC Agent Fleet to GitHub

## Two access channels

Like Asana, GitHub access splits:

- **Agent runtime → GitHub MCP server** (`https://api.githubcopilot.com/mcp/`). Uses a **GitHub App** installation token, OR a PAT stored at `/sdlc-agents/github-mcp-token`. GitHub's official MCP supports both.
- **CI (deploy workflow) → AWS** (no GitHub side needed beyond OIDC). The deploy role is assumed via GitHub Actions OIDC. No secret stored in GitHub beyond `AWS_DEPLOY_ROLE_ARN` and `AWS_ACCOUNT_ID`.

## Decide: PAT or GitHub App?

Ask the user which they prefer:

| | PAT | GitHub App |
|---|---|---|
| **Setup time** | ~2 min | ~15 min |
| **Scope** | user-wide (acts as the PAT owner) | repo- or org-scoped (acts as the App) |
| **Rate limits** | 5000 req/hr per user | 5000+ per installation, better for high-volume |
| **Revocation** | manual token delete | single click in org settings |
| **Org SSO** | need to "authorize" the PAT for each SSO-protected org | works cleanly with SSO |
| **Right for** | demos, single-repo projects, solo dev | production, orgs with SSO, multi-repo |

For a demo, PAT is fine. For production, push toward GitHub App.

## Path A — PAT (fast path)

1. User goes to https://github.com/settings/personal-access-tokens/new (fine-grained, preferred) or https://github.com/settings/tokens/new (classic)
2. For fine-grained: select the target repo(s), grant **Contents: Read**, **Issues: Read and Write**, **Pull requests: Read and Write**, **Metadata: Read**. For Docwriter-style agents that open doc PRs, also grant **Contents: Read and Write**. For agents reading releases: **Code: Read**.
3. For classic: `repo` scope is the blunt-but-working option.
4. If the repo is in an SSO-protected org, the user must click **Configure SSO** on the PAT and authorize it for the org.
5. Copy the token, store in SSM:

   ```bash
   read -r GH_TOKEN
   aws ssm put-parameter \
     --name /sdlc-agents/github-mcp-token \
     --value "$GH_TOKEN" \
     --type SecureString \
     --region "$REGION" \
     --overwrite
   unset GH_TOKEN
   ```

## Path B — GitHub App (production path)

1. User goes to https://github.com/organizations/<org>/settings/apps → **New GitHub App**
2. Fill the required fields:
   - Name: `SDLC Agent Fleet (<stage>)`
   - Homepage: any URL
   - Webhook: can be disabled for MCP-only usage (the agent-dispatch workflow is what listens for `@agent` mentions, not this app)
   - Permissions (Repository): Contents Read & Write, Issues R&W, Pull requests R&W, Metadata Read
   - Permissions (Organization): Members Read (optional, for routing by team)
3. Generate a private key. Download the PEM.
4. Install the app on the target repos.
5. Store the App ID, installation ID, and PEM in SSM:

   ```bash
   aws ssm put-parameter --name /sdlc-agents/github-app-id --value "<app_id>" --type String --region "$REGION" --overwrite
   aws ssm put-parameter --name /sdlc-agents/github-app-installation-id --value "<installation_id>" --type String --region "$REGION" --overwrite
   aws ssm put-parameter --name /sdlc-agents/github-app-private-key --value "$(cat app.pem)" --type SecureString --region "$REGION" --overwrite
   rm app.pem  # don't leave it on disk
   ```

6. The agents mint installation tokens at runtime via JWT signed with the PEM; existing tool code in `agents/*/tools/github_mcp.py` reads either SSM shape.

## Verify

```bash
# Reads $REGION from the environment — same AWS region used in
# sdlc-agents-provision-aws (recorded at .sdlc-agents/selection.yaml → aws.region).
python3 <<'PY'
import os, boto3, requests
ssm = boto3.client("ssm", region_name=os.environ["REGION"])
# Path A: PAT
try:
    tok = ssm.get_parameter(Name="/sdlc-agents/github-mcp-token", WithDecryption=True)["Parameter"]["Value"]
    r = requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {tok}", "Accept":"application/vnd.github+json"}, timeout=15)
    print(f"PAT user: {r.status_code} {r.json().get('login','?')}")
    r = requests.get("https://api.githubcopilot.com/mcp/", headers={"Authorization": f"Bearer {tok}","Accept":"application/json, text/event-stream"}, timeout=15)
    print(f"MCP: {r.status_code}")
except Exception as e:
    print(f"PAT path not configured: {e}")

# Path B: GitHub App — skipped here unless the user set it up
PY
```

MCP should return 200 or a JSON-RPC response on POST. A 401 means the token isn't MCP-enabled (GitHub's MCP requires tokens with specific scopes; fine-grained tokens must have at least "Contents" and "Issues").

## Configure CI OIDC

Separate from the MCP credential: the deploy workflows assume an AWS role via OIDC. The OIDC provider and deploy role are **not** created by the foundation stack — `sdlc-agents-provision-aws` Step 0 creates them manually (one-time per AWS account). By the time you're in this skill, that role exists and its ARN was captured as `$DEPLOY_ROLE_ARN`. The target GitHub repo needs:

- Secret `AWS_DEPLOY_ROLE_ARN` — the role ARN from `sdlc-agents-provision-aws` Step 0b
- Secret `AWS_ACCOUNT_ID` — the 12-digit account ID

Both are already written by `sdlc-agents-provision-aws` Step 5 if you ran it first. If the user skipped that step or set them manually, confirm they exist here.

Walk the user through:
- Repo → Settings → Secrets and variables → Actions → New repository secret
- Add both

No AWS credentials stored in GitHub. The OIDC trust only permits `token.actions.githubusercontent.com` for the configured repo.

## Record GitHub state

Append to `.sdlc-agents/selection.yaml`:

```yaml
github:
  auth_mode: pat        # or: app
  owner: <owner>        # the user's GitHub org or username
  repo: <repo>          # the target repository name
  default_branch: main
```

(`owner/repo/default_branch` are used by `docwriter` and `adr`, and by any future agent that opens PRs.)

## Use GitHub for PM too? (Issues + Projects V2)

Everything above wires GitHub for **source control / dev** (Contents, Issues,
PRs). GitHub can *also* be the **project-management backend** for `workitems`
**and `researcher`** — GitHub Issues + Projects V2 instead of Asana. Do this
section only when the user picked GitHub as their PM tool during selection
(`toolchain.pm == github` in `.sdlc-agents/selection.yaml`). If they're on
Asana, skip this entirely; the SCM setup above is all GitHub needs.

This is additive to the SCM credential — you reuse the **same** PAT or App,
just with added scopes and a couple of runtime env vars.

### 1. Set the PM runtime env vars

When GitHub is the PM backend, the `workitems` and `researcher` agents run with
`PM_BACKEND=github` (`project_config.py` reads it; default is `asana`). In github
mode they read **no** Asana vars and instead require:

- `PM_BACKEND=github`
- `GITHUB_REPO` — `owner/repo` of the repository whose issues are tracked
- `GITHUB_PROJECT_NUMBER` — the Projects V2 board number (see step 3)
- `GITHUB_PROJECT_OWNER` — **only if** the board owner differs from the repo
  owner (e.g. an org-level board over a repo in a different namespace). If the
  board lives under the same owner as the repo, omit it.

Set these on **each** PM-capable agent's AgentCore runtime that you deployed
(`workitems`, and `researcher` if selected) — the same place the runtime's
other environment is configured. They are configuration, not secrets — no SSM
needed. (`docwriter` and `adr` don't use `PM_BACKEND`; leave it unset for them.)

### 2. Add the Projects scopes to the token

The PM path needs the GitHub Projects V2 API, which is **not** covered by the
Contents/Issues/PRs scopes from the SCM setup. Add:

- **Classic PAT:** add `read:project` (read) and `project` (read + write) scopes.
- **Fine-grained PAT / GitHub App:** add the **Projects** permission —
  **Read** for read-only, **Read and write** to let the agent move cards /
  update status. (These are *in addition to* the Contents/Issues/PRs/Metadata
  permissions above.)

GitHub's remote MCP server exposes Projects V2 through an opt-in `projects`
toolset. The agent enables it per-connection by sending the header
`X-MCP-Toolsets: default,projects` — **it does this automatically**. The operator
only has to make sure the token carries the Projects scope above; no MCP-side
config is required.

### 3. Identify the Projects V2 board number and owner

Open the board in GitHub and read the number straight out of the URL:

- Org-owned board: `https://github.com/orgs/<org>/projects/<number>` →
  `GITHUB_PROJECT_NUMBER=<number>`, `GITHUB_PROJECT_OWNER=<org>`.
- User-owned board: `https://github.com/users/<user>/projects/<number>` →
  `GITHUB_PROJECT_NUMBER=<number>`, `GITHUB_PROJECT_OWNER=<user>`.

The owner is the org or user in the URL path. Set `GITHUB_PROJECT_OWNER` only
when it differs from the repo owner (step 1).

### 4. Register the GitHub PM webhook

Issue-comment events already flow through the `agent-dispatch.yml` Actions
workflow. The PM backend additionally needs **issue-assignment** and **board
status-change** events, which that workflow does not carry — they come from the
GitHub PM webhook (`github-webhook-${STAGE}` Lambda, route `/github/webhook`,
delivering `issues.assigned` and `projects_v2_item` events). Register it:

```bash
python scripts/bootstrap_github_webhook.py \
  --region "$REGION" --stage "$STAGE" --repo <owner>/<repo>
```

Also set the bot-login env var **`WORKITEMS_GH_BOT_LOGIN`** on the workitems
runtime to the GitHub login the agent posts as. Issue-assignment triggers
resolve to `workitems` by matching the assignee against this login — without it,
assignment events won't route to the agent.

### 5. Record the PM backend in selection.yaml

Append a top-level `pm:` block to `.sdlc-agents/selection.yaml` (and ensure
`toolchain.pm: github` is set):

```yaml
pm:
  backend: github
  github_project_number: 7        # the Projects V2 board number
  github_project_owner: my-org    # org or user that owns the board
```

### Production note (T-11)

The added `project` scope widens a PAT even further — a classic PAT with
`project` can read and write **every** Projects V2 board its owner can reach,
not just this one. Consistent with the threat model's **T-11** guidance (prefer
a scoped GitHub App over a broad PAT), steer production users to the **GitHub
App** path with the Projects permission scoped to the target org/repos rather
than a broad classic PAT.

## What this skill does NOT do

- Configure the `agent-dispatch.yml` workflow triggers. That's `sdlc-agents-register-triggers`.
- Install Claude Code Action for `@claude` in issues. That's a separate Anthropic-provided action, not part of this fleet.
- Handle GitLab. If the user is on GitLab, use `sdlc-agents-connect-gitlab` (not yet written — flag it as a gap if asked).

---
name: sdlc-agents-connect-atlassian
description: Use when the user needs to connect SDLC agents to Atlassian (Jira and/or Confluence). Walks through creating the service account + scoped API token, connecting the site in the dashboard, deploying/installing the atlassian-events Forge forwarder, onboarding projects/spaces, granting access, and verifying delivery. Knows the specific Atlassian pitfalls (scoped vs classic tokens, cloud id resolution, Forge dev-account requirement, propose-mode Confluence writes).
---

# Connect the SDLC Agent Fleet to Atlassian (Jira + Confluence)

## One foundation, two products

One site connect covers BOTH products (atlassian-connector spec Part A): one
service account, one API token, one Forge app install per site, per-product
enable toggles. Almost everything happens **in the dashboard** — the only CLI
step is the one-time Forge forwarder deploy, which `deploy_fleet.py` already
runs when the Forge CLI is authenticated.

Two channels, deliberately separate:

- **Events IN** — the `atlassian-events` **Forge app** forwards Jira/Confluence
  events to the fleet's webhook API, authenticated per delivery by a Forge
  Invocation Token (RS256 vs Atlassian's JWKS). **No webhook secret exists** —
  nothing to paste, store, or rotate.
- **REST OUT** — the fleet's **service-account API token** (SSM SecureString,
  one per site) does every read/write: context fetches, replies, agent tools.
  The Forge app never touches REST.

## Step 1 — Service account + scoped API token (user, in Atlassian)

1. Have an org admin create a dedicated account, e.g. `sdlc-agents@<org>.com`,
   display name **SDLC Agents** (this is the mention anchor — `@SDLC Agents`
   becomes real in both products' pickers).
2. Grant it the target **projects' work permissions** (browse, comment, edit
   issues, transition) and the target **spaces' page permissions** (view, add
   comment, add page for direct-mode spaces).
3. Logged in AS that account: https://id.atlassian.com/manage-profile/security/api-tokens
   → **Create API token with scopes** (max lifetime 1 year — note the expiry
   date; the dashboard shows a countdown and alerts at 14/3/0 days).
   Classic unscoped tokens work but are flagged in the UI — prefer scoped.

Pitfall: the token must be minted while logged in as the SERVICE account, not
the admin's own account — the token's identity is the comment/page author and
the bot-loop filter.

## Step 2 — Deploy the Forge forwarder (operator, once per stage)

Usually already done: `scripts/deploy_fleet.py` deploys it when the Forge CLI
is available. To run/repair it directly:

```bash
npm i -g @forge/cli
forge login          # any Atlassian developer account the org controls
python scripts/deploy_forge_atlassian.py --stage "$STAGE" --region "$REGION"
```

The script registers the app on first run, wires `FLEET_WEBHOOK_BASE`, deploys,
and publishes the app id + private install link to SSM — the dashboard's Sites
tab reads them, so admins never need the CLI output. The forwarder is
site-agnostic (cloud id from the invocation context): new sites need an app
INSTALL, never a re-deploy.

Pitfalls:
- `forge login` needs an Atlassian **developer** account (any Atlassian account
  works; it just owns the app registration). Use a team-owned account, not a
  personal one that may be deactivated.
- A manifest change later prompts admins for upgrade consent on every installed
  site — expected, not an error.

## Step 3 — Connect the site (admin, in the dashboard)

Dashboard → **Connectors → Atlassian → Sites**:

1. Paste site URL (`https://<org>.atlassian.net`), the service-account email,
   and the API token → **Connect**. The backend verifies the token
   (`/rest/api/3/myself`), resolves the cloud id + bot accountId, stores the
   token as a SecureString, and writes the site row `active`.
2. Toggle **Jira** / **Confluence** on per what you're onboarding.
3. Click the **install link** on the app-install card → install the Forge app
   on the site (scopes + egress shown up front). One install covers both
   products.
4. Comment on any issue or page, then **Verify delivery** per product — green
   check = `webhook_last_seen` is stamping.

Pitfall: if the cloud id can't be resolved automatically, fetch it yourself at
`https://<site>.atlassian.net/_edge/tenant_info` and pass it as `site_id`.

## Step 4 — Onboard containers + grant access (admin, in the dashboard)

- **Jira projects** tab: add e.g. `ENG` as `allow`, attach its linked repos
  (the co-scope a Jira dispatch may reach with GitHub tools).
- **Confluence spaces** tab: add e.g. `DOCS` as `allow`. **Onboarding a space
  makes it readable by ALL agents** — never onboard a confidential space.
  Leave `write_mode: propose` (default) until you trust the loop; flip to
  `direct` later (one toggle, no deploy).
- **Access rules** tab: grant a permission group (or user) per product —
  "may trigger from Jira" and "may trigger from Confluence" are separate
  rules. Use **Test access** to confirm before announcing.
- Optional: **Automations** tab — e.g. event `issue_transitioned`, to-status
  `Code Review`, agent `adr`.

## Step 5 — Verify end-to-end

On a Jira issue in an onboarded project: `@SDLC Agents workitems summarize
this ticket` (mention via the picker). Expect a threaded "🏁 on it" ack, then
the result. Same on a Confluence page comment with `docwriter`.

First-touch users get the onboarding reply; approve them in Access → Users.
Users opt into DMs with `/sdlc-notify me` in Slack.

Inline verification probe (REST channel, no dispatch):

```bash
TOKEN=$(aws ssm get-parameter --name "/sdlc-agents/$STAGE/atlassian/$SITE_ID/api-token" --with-decryption --region "$REGION" --query 'Parameter.Value' --output text)
curl -s -u "sdlc-agents@<org>.com:$TOKEN" "https://<site>.atlassian.net/rest/api/3/myself" | jq '{accountId, displayName}'
unset TOKEN
```

## Known errors and fixes

| Error | Cause | Fix |
|---|---|---|
| Connect fails "Atlassian rejected the token (HTTP 401)" | Wrong email/token pair, or token minted under the admin's account with 2FA-gated org policy | Re-mint the token logged in as the service account; check org token policies |
| Verify delivery shows no deliveries | Forge app not installed on the site, or installed to the wrong environment | Re-open the install link; confirm the environment matches the stage (dev→development, prod→production) |
| Mention does nothing, no ack | Sender not granted (WHO) or project/space not onboarded (WHERE) | Access rules tab + Test access simulator; check Activity for `TriggerDenied` |
| Agent reads fail "space not onboarded" | Space allow row missing — Confluence READS are container-scoped, unlike GitHub | Onboard the space (understanding the fleet-wide-read consequence) |
| `update_page` rejected "page changed since you read it" | Stale `base_version` — a human edited concurrently | Expected (T-57); the agent re-reads and retries |
| Writes rejected in a space that should allow them | `write_mode: propose` (default) or `write_agents` narrowing | Flip the space to `direct`, or add the agent to write_agents |
| Both products go silent on a site | Forge app uninstalled (silences BOTH products) | Sites tab shows stale liveness; re-install from the install card |

## Record state

Append to `.sdlc-agents/selection.yaml`:

```yaml
atlassian:
  site_id: <cloud-id>
  site_url: https://<org>.atlassian.net
  products: {jira: true, confluence: true}
  projects: [ENG]
  spaces: [DOCS]
  token_expires: <YYYY-MM-DD>
```

## What this skill does NOT do

- Create the Atlassian service account or grant its product permissions — an
  org admin does that in Atlassian's admin UI.
- Set up OAuth 3LO (`scripts/bootstrap_jira_oauth.py` is the documented
  alternative for orgs that forbid service accounts — spec §A6.2).
- Onboard Jira Server / Data Center — Cloud only.
- Author Jira-side Automation rules (we react to events; we don't create Jira
  Automation).

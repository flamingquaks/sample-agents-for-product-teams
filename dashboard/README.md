# Fleet Monitoring Dashboard (SPA)

A React + Vite single-page app that gives operators a fleet-wide view of agent
runs: live status, filtering, per-run detail, and cross-agent traceability. It
reads the operator-authorized query API (`infra/dashboard/`) and authenticates
operators via the Cognito Hosted UI.

This is the **frontend only**. The API, Cognito pool, and CloudFront/S3 hosting
are provisioned by the foundation SAM stack behind the `DeployDashboard=true`
flag.

## Views

The SPA has four views, navigable via in-memory state (no router dependency):

| View | What it shows |
|------|---------------|
| **Fleet** | Paginated list of all runs newest-first, with status pills, agent/source labels, and trace chips. Supports filtering by agent, status, source, and requester. |
| **Run Detail** | Full record for one run: timestamps, duration, token usage, cost, participants, source link, and all trace refs. Reached by clicking a run row. |
| **Trace** | All runs sharing a trace dimension (e.g. every agent's work on a branch or Jira key). Reached by clicking any trace chip. |
| **Admin** | Fleet configuration (admins only): onboard/remove agent **capabilities** and the repositories the fleet may act on, toggle each repo's multi-repo eligibility, flip the fleet-wide "restrict to allowlist" setting, and manage **Connectors** — the GitHub App manifest setup, Slack workspaces/channels, **trigger rules** (the AVP trigger-authz grant data), and the channel-onboarding **request** approve/deny queue. Reached via the **Admin** nav button, shown only to members of the `admins` group. |

**Trace chips** are data-driven — whatever keys appear in a run's `trace_refs`
are rendered as clickable chips. Adding a new integration dimension (repo,
branch, PR, Jira key, Asana task, etc.) requires no UI change.

## Query API

The SPA consumes four read-only endpoints served by `infra/dashboard/api.py`.
All require a valid Cognito token with `operators` (or `admins`) group membership.

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/runs` | Fleet list, newest-first. Optional query params: `limit` (1–100, default 25), `next_token`, `agent_id`, `status`, `source`, `requester`. |
| GET | `/runs/{assignment_id}` | Full detail for one run. |
| GET | `/trace?dim=<dimension>&value=<value>` | Runs sharing a trace dimension. Allowed dimensions: `repo`, `branch`, `pr_number`, `pr_url`, `issue_number`, `jira_key`, `asana_task_gid`, `project_gid`, `project_name`. |
| GET | `/stats` | Fleet rollups: total, active, by_status, by_agent, by_source. Used by the dashboard header. |

## Admin API

The Admin view consumes the write endpoints served by `infra/dashboard/admin.py`.
All require a valid Cognito token with **`admins`** group membership (fails
closed — operators can view but not configure).

| Method | Route | Purpose |
|--------|-------|---------|
| GET/POST | `/admin/repos` | List / onboard-update a repo. POST body: `repo` (`owner/repo`), optional `enabled`, `multi_repo_eligible`. |
| DELETE | `/admin/repos/{repo}` | Remove a repo from the fleet. |
| GET | `/admin/settings` · PUT `/admin/settings` | Get / update fleet settings (`restrict_repos`). |
| GET/POST | `/admin/capabilities` | List / onboard an agent capability (starts the shared build → runtime deploy). |
| DELETE | `/admin/capabilities/{agent_id}` | Offboard a capability and republish the registry. |
| GET | `/admin/github-app/status` | GitHub App configured?/slug/install URL. |
| GET | `/admin/github-app/setup/manifest` | Build the App-registration manifest (webhook → dispatch API). |
| POST | `/admin/github-app/setup/callback` | Exchange the manifest code to create the App. |
| GET/POST | `/admin/slack/workspaces` | List / onboard a Slack workspace (`team_id`, `default_channel_policy`). |
| DELETE | `/admin/slack/workspaces/{team_id}` | Remove a Slack workspace. |
| GET/POST | `/admin/slack/channels` | List / set a per-channel allow\|deny policy. |
| DELETE | `/admin/slack/channels/{team_id}/{channel_id}` | Remove a channel policy row. |
| GET/POST | `/admin/trigger-rules` | List / author a trigger-authz grant (`connector`, `subject_type`, `subject_id`, `agent_id`, `workspace`, `effect`). This is the AVP `TriggerPolicyStore` grant *data*. |
| DELETE | `/admin/trigger-rules/{rule_id}` | Revoke a trigger grant. |
| POST | `/admin/trigger-rules/simulate` | Dry-run an authorization decision against the current grants + channel posture. |
| GET | `/admin/channel-requests` | List channel-onboarding requests (optional `status`). |
| POST | `/admin/channel-requests/{request_id}/approve`\|`/deny` | Decide a channel-onboarding request (approve writes the allow row + grant). |

**Repo onboarding** writes the config row `pending`, syncs the Gateway Cedar
policy, then marks it `active` — so the dispatch allowlist never widens ahead of
the tool-call policy. A policy-sync failure leaves the row `pending` and returns
`502`. See `docs/aws-deploy.md` § AgentCore Gateway.

**Trigger rules** are the *data* the Dispatch Router's AVP trigger authz reads
(`TriggerPolicyStore`); the Cedar policy set is fixed, so granting a subject is a
DynamoDB write, not a new policy. See [`docs/specs/slack-connectors-spec.md`](../docs/specs/slack-connectors-spec.md).

## Architecture notes

- **One build, any stack.** The bundle reads runtime config from `/config.json`
  at startup (written at deploy time from stack outputs), falling back to Vite
  env vars for local dev. Nothing stack-specific is baked into the build. See
  `src/config.ts`.
- **Auth:** Authorization Code + PKCE against the Cognito user pool via
  `react-oidc-context`. The access token carries the `cognito:groups` claim the
  API checks; tokens are held in `sessionStorage` (cleared when the tab closes).
  See `src/auth.ts`.
- **Adaptive polling:** fast (5 s) while runs are active, idle (20 s) otherwise.
  Pauses on a hidden tab and refreshes immediately on visibility. No websockets.
  See `src/hooks.ts`.
- **Data-driven trace chips:** whatever `trace_refs` keys a run carries are
  rendered as chips, so a new integration's dimension shows up with no UI change.

## Local development

Deploy the foundation stack with `DeployDashboard=true`, then:

```bash
cd dashboard
cp .env.example .env.local     # fill in from the stack outputs (see below)
npm install
npm run dev                    # http://localhost:5173
```

### .env.local variables

| Variable | Stack output | Example |
|----------|--------------|---------|
| `VITE_API_BASE_URL` | `DashboardApiEndpoint` | `https://abc.execute-api.us-west-2.amazonaws.com/dev` |
| `VITE_COGNITO_AUTHORITY` | Derived: `https://cognito-idp.<region>.amazonaws.com/<DashboardUserPoolId>` | `https://cognito-idp.us-west-2.amazonaws.com/us-west-2_XXXXXXXXX` |
| `VITE_COGNITO_CLIENT_ID` | `DashboardUserPoolClientId` | `xxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `VITE_COGNITO_LOGIN_DOMAIN` | `DashboardLoginDomain` | `https://sdlc-agents-dash-000000000000-dev.auth.us-west-2.amazoncognito.com` |
| `VITE_REDIRECT_URI` | — | `http://localhost:5173/` |

`http://localhost:5173/` must be registered as a callback + logout URL on the
Cognito app client (the SAM template seeds it). Your Cognito user must be in the
`operators` group or the API returns 403.

## Build

```bash
npm run build        # tsc typecheck + vite production build → dist/
npm run preview      # serve the built bundle locally
```

## Deployment

The `deploy-dashboard.yml` GitHub Actions workflow handles CI/CD:

1. **Trigger:** pushes to `main` touching `dashboard/` or the workflow file itself (also manual dispatch).
2. **Read stack outputs:** fetches `DashboardSiteBucketName`, `DashboardDistributionId`, `DashboardApiEndpoint`, `DashboardUserPoolId`, `DashboardUserPoolClientId`, `DashboardLoginDomain`, and `DashboardUrl` from the `sdlc-agents-<stage>` CloudFormation stack.
3. **No-op guard:** if the dashboard outputs are absent (stack deployed with `DeployDashboard=false`), the job exits early with a notice.
4. **Build:** `npm ci && npm run build` produces `dist/`.
5. **Generate config.json:** writes the runtime config from the stack outputs into `dist/config.json`.
6. **Upload to S3:** hashed assets cached immutably; `index.html` and `config.json` served with `no-cache`.
7. **Invalidate CloudFront:** a `/*` invalidation ensures the new bundle is live immediately.

## Operator & admin onboarding

Two Cognito groups back the dashboard:

- **`operators`** — view the Fleet / Run / Trace surfaces (read API).
- **`admins`** — additionally configure the fleet via the Admin view (write API): onboard repos, set multi-repo eligibility, toggle restriction. Admins can view everything operators can.

To grant a user access:

1. Create the user in the dashboard's Cognito user pool (the `DashboardUserPoolId` output).
2. Add the user to **`operators`** (viewers) and/or **`admins`** (fleet configurers). Without a group the API returns 403 and the UI shows a friendly error banner.
3. Share the dashboard URL (`DashboardUrl` stack output) with the user.

Group management uses the AWS Console (Cognito → User Pools → the dashboard pool → Groups → `operators`/`admins` → Add user) or the AWS CLI:

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <DashboardUserPoolId> \
  --username <user-email-or-sub> \
  --group-name operators   # or: admins
```

Once at least one admin exists, they onboard the repositories the fleet may act
on in the **Admin** view — the fleet is multi-repo, so no repo is baked in at
deploy time.

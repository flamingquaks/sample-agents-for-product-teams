# Connector: Jira
## First-class Jira source — dispatch, agent tools, traceability, event automation, and per-user Slack notifications

> **Status: PROPOSED.** This spec onboards **Jira Cloud** as a first-class fleet connector at (and beyond) parity with Slack/GitHub/Asana. It covers five capability pillars:
>
> 1. **Jira as a dispatch source** — `@sdlc-agents <agent> …` mentions and conversations on Jira issues, routed through the existing dispatch spine (identity → AVP trigger authz → guardrail → runtime), with results posted back as issue comments (§3, §6, §7).
> 2. **Agent access to Jira data** — a curated `JiraTarget` on the AgentCore Gateway (Lambda broker, GitHub-style), Cedar-scoped per agent so *specific* agents may update/transition tickets while others are read-only (§8).
> 3. **Traceability** — native `jira_key` trace refs, agent-run ↔ issue remote links, and cross-platform linking conventions (§9).
> 4. **Event automation** — a new data-driven **automation-rule engine** ("when an issue moves to *Code Review*, run agent *adr* on it"), source-agnostic by schema, Jira-first (§10).
> 5. **Notifications** — Jira events in the existing channel subscription system **plus new per-user Slack DM preferences** ("DM me when an agent comments on my issue / replies to me") (§11).
>
> Plus a **first-class onboarding experience**: a Connectors → Jira admin page with a guided connect flow, project onboarding, access rules, an automations tab, and a `sdlc-agents-connect-jira` quickstart skill (§12, §15).
>
> **Transport revision (2026-07-24):** the original draft used an admin-registered Jira System WebHook with a stored HMAC secret. That transport is **superseded by the shared `atlassian-events` Forge forwarder app** defined in `confluence-connector-spec.md` §6.1 — one Forge app, installed once per Atlassian site, forwards both Jira and Confluence events to the fleet's receivers, each delivery verified via the Forge Invocation Token against Atlassian's published JWKS. This deletes the manual webhook-registration step, the per-site webhook secret, and the stored-HMAC threat surface (§6.1, §18 T-46/T-47 revised). Everything downstream of the receiver is unchanged.
>
> Prior art already in-repo (built on, not duplicated): `scripts/bootstrap_jira_oauth.py` (Atlassian 3LO bootstrap — kept as an *alternative* path, §5.2), `agents/workitems/tools/jira_mcp.py` (dead pre-gateway direct-MCP helper — **deleted** by this spec; direct-to-vendor violates the gateway-only rule), the `jira_key` regex extraction in `infra/dispatch/enrichment.py`, `jira_key` in `queries.TRACE_DIMENSIONS`, and the roadmap item "Jira + GitLab support".

---

## 1. Goals & non-goals

### Goals

1. **Jira as a first-class source.** A user `@mentions` the fleet on a Jira issue comment; the agent does the work; the reply lands back **on the same issue** as a comment — the same UX GitHub, Asana, and Slack already have. Follow-up mentions continue the conversation (full comment history is front-loaded into the dispatch, GitHub-style).
2. **Agents read and act on Jira.** Every agent that needs it can read issues, comments, transitions, and search via JQL; **write access is per-agent** (Cedar-scoped): `workitems` may create/update/transition/assign issues; `adr` and `docwriter` may only comment/link; nothing may ever delete (structurally absent from the target schema, like GitHub's `DESTRUCTIVE_TOOLS`).
3. **Traceability.** Every Jira-originated run carries native `jira_key`/`jira_project`/`jira_site` trace refs; agents stamp Jira issues with remote links back to the PRs/runs they produced; the dashboard trace view joins runs across GitHub/Asana/Slack/Jira on `jira_key` (the dimension already exists).
4. **Event automation.** Admins author rules like *"project ENG: issue transitioned → 'Code Review' ⇒ run `adr` with 'Review the code for {{issue_key}}'"*. Rules are **data** (DynamoDB rows), matched by the receiver, dispatched through the full authz/guardrail spine under an auditable synthetic principal — never a bypass.
5. **Notifications.** Jira events join the channel-subscription catalog, and — new surface — **per-user Slack DM preferences**: a person can opt in to be DM'd when an agent comments on an issue they reported/watch/are assigned to, or replies to/mentions them. Tiered, threaded, identity-resolved, fail-quiet.
6. **First-class onboarding.** A Connectors → Jira page with a guided connect (service-account + API token paste, mirroring Slack's one-step connect), project onboarding, per-connector access rules + simulator, and a Claude Code connect skill. Multi-site from day one (mirroring multi-workspace Slack).
7. **No regressions.** GitHub/Asana/Slack dispatch and the existing test suite keep passing. The Jira receiver is always deployed but **inert until a site is onboarded** (the Slack §19 posture — fail-closed at runtime, enable via Admin; no deploy flag).

### Non-goals

- **Jira Server / Data Center.** Jira Cloud only (REST v3, Atlassian account ids). DC has different auth, webhooks, and user ids; out of scope.
- **Confluence / Compass.** The Atlassian OAuth app *could* cover them; this spec is Jira-only. The site/credential rows are named `jira_*` deliberately — a Confluence connector would be its own connector page over the same site record.
- **Jira-side automation authoring.** We do not create Jira Automation rules inside Jira; the fleet's automation engine (§10) listens to webhooks. (Jira-side rules can still *cause* events we react to.)
- **Agile-board write operations** (sprint moves, backlog ranking) in v1 — read-only board/sprint context is a fast-follow; the broker tool set is curated and extendable.
- **Replacing Asana.** Jira is an *additional* PM surface; the Asana connector is untouched.

---

## 2. Where this fits in the existing architecture

Everything reuses an existing seam; the only genuinely new *concept* is the automation-rule engine (§10) and per-user notification prefs (§11.3).

| Piece | Existing seam reused | New for Jira |
|---|---|---|
| Webhook receiver | Thin adapter over `infra/dispatch/mentions.py` (`RegistryCache` mention resolution), async `Event`-invoke of the router — same as `github_webhook.py` / `asana_webhook.py` / `slack_webhook.py` | `infra/dispatch/jira_webhook.py`, route `POST /jira/webhook/{site}`, fed by the **shared `atlassian-events` Forge forwarder** (confluence-connector-spec §6.1; adopted here in §6.1) |
| Trigger authz | AVP `TriggerPolicyStore` fixed 3-policy set; grants as `trigger_rule` data (`trigger_authz.py` + `trigger_grants.py`) — **zero Cedar changes** | principal prefix `jira:<accountId>`; connector enum `"jira"`; project WHERE-axis rows (§5.3) |
| Identity | `infra/dispatch/identity.py` get-or-create + first-touch onboarding gate | `handles.jira = <accountId>` (account ids are global across Atlassian sites — a plain string, not a per-site map); verified email from Jira user API |
| Agent tools | AgentCore Gateway Lambda target + broker (`GitHubTarget`/`scm_broker.py` pattern), Cedar `AGENT_TOOL_GRANTS` in `infra/dashboard/fleet_policy.py`, `policy_sync.py` | `JiraTarget` + `infra/dispatch/jira_broker.py` + `JIRA_TOOL_CLASS` (§8) |
| Replies | `infra/dispatch/reply.py` (`_post_block_reply` branch) | `post_jira_comment(site, issue_key, body)` |
| Trace refs | `enrichment.derive_trace_refs` open map; `jira_key` regex + `TRACE_DIMENSIONS` already exist | native `source == "jira"` branch |
| Notifications | `notify.py` fan-out, `slack_notify.py` modal, `notif_sub#` rows, `assignment_notifier.py` stream Lambda | Jira events in `TIER_EVENTS`; per-user `notif_pref#` rows + DM delivery (§11) |
| Admin UI | `dashboard/src/connectors/registry.ts` descriptor + page component; `TriggerRulesPanel`, `ActivityPanel`, simulator | `JiraConnectorPage.tsx` (§12) |
| Admin API | `infra/dashboard/admin.py` `_route` + `config_store` kinds via `kind-index` GSI | `/admin/jira/*`, `/admin/automation-rules*`, `/admin/notif-prefs*` (§13) |
| Automation | *(nearest precedents only: Asana assignment/custom-field maps, `github_webhook._notify_scm` event mapping)* | **new** `automation_rule#` engine (§10) |

**Enum/branch points that must learn `"jira"`** (the exhaustive checklist, from code survey): `config_store.TRIGGER_CONNECTORS` + `IDENTITY_SOURCES` (mirrored in `identity.py`), `router.namespaced_principal`, `router._post_block_reply`, `enrichment.derive_trace_refs`/`derive_participants`, `assignment_notifier._actor_from`, `reply.py`, `dashboard/src/types.ts` (`TriggerRule.connector`, `Identity.handles`, `CapabilityConfig.triggers` comment), `dashboard/src/connectors/registry.ts`, `dashboard/src/format.ts::sourceLink` (build `https://<site>.atlassian.net/browse/<KEY>` from `jira_key` + `jira_site`), `dashboard/src/App.tsx` (route `#/connectors/jira`), `infra/foundation/template.yaml` (function, routes, alarms, target).

---

## 3. End-to-end flows

### 3.1 Happy path (issue-comment mention)

```
User comments on ENG-142:  @sdlc-agents workitems break this into subtasks
  │
  ▼  Forge product trigger → atlassian-events app → POST /jira/webhook/{site}
     (API Gateway → jira-webhook Lambda)
jira_webhook.handler
  1. resolve {site} path param → jira_site row (onboarded? enabled?)
  2. verify the Forge Invocation Token (RS256 vs Atlassian's JWKS: signature, expiry,
     audience, pinned app id) + cloud-id ↔ site-row cross-check; reject bad/missing/expired
  3. dedup on event id; ignore events authored by the fleet service account (bot-loop)
  4. event = comment_created → mention scan: ADF mention node for the fleet account,
     or text fallback "@sdlc-agents" / "@<agent>" → resolve agent via RegistryCache
  5. sender = "jira:<accountId>"; email + displayName from the webhook's comment.author
     (Atlassian-authenticated directory ⇒ email_verified=True when present)
  6. context = {site, project_key:"ENG", issue_key:"ENG-142", issue_summary, issue_status,
                issue_type, comment_id, requester_email?, issue_comments:[…full history…]}
  7. async Event-invoke dispatch-router; return 200
  │
  ▼
router.handler                                    (all existing machinery, unchanged order)
  8. resolve agent → identity.resolve("jira", accountId, …) → onboarding gate
  9. trigger_authz.is_authorized(principal="jira:<accountId>", agent="workitems",
       context={source:"jira", workspace:<site>, channel:"", channelAllowed:project_ok})
 10. project binding (§5.3), concurrency, guardrail (unchanged)
 11. create_assignment (trace_refs: jira_key/jira_project/jira_site), invoke_agent
 12. reply.post_jira_comment(site, "ENG-142", "🏁 @workitems is on it — run <id>")
  │
  ▼
agent runtime → gateway JiraTarget tools → posts result via JiraTarget___add_comment
               → adds remote links to any PRs/issues it created (§9.2)
```

### 3.2 Automation path ("issue moved to Code Review → run adr")

```
Jira: ENG-142 transitions  In Progress → Code Review
  │
  ▼  Forge trigger (issue updated, changelog.items[field=="status"]) → POST /jira/webhook/{site}
jira_webhook.handler
  1–3. verify / dedup / bot-loop guard (as §3.1)
  4. no mention → automation match: automation_rules.match(
       connector="jira", event="issue_transitioned",
       facts={site, project:"ENG", from_status:"In Progress", to_status:"Code Review",
              issue_type:"Story", labels:[…]})
  5. rule hit → per-rule cooldown/dedup check (same issue+rule+status within TTL → skip)
  6. dispatch payload: agent_id from the rule, instruction = rendered template
     ("Review the code for ENG-142: <summary>"), sender = "automation:jira:<rule_id>",
     trigger_type = "automation"
  │
  ▼
router.handler — SAME spine: the automation principal must hold a trigger_rule grant
  (auto-authored at rule creation, §10.4); guardrail runs on the rendered instruction;
  concurrency caps apply; the assignment row records trigger_type="automation" +
  trace_refs.automation_rule_id — fully auditable, never a side door.
```

### 3.3 Per-user notification path ("agent replied to me")

```
Agent posts a comment on ENG-142 (via JiraTarget___add_comment)
  │
  ▼  Forge trigger comment_created (author == fleet service account) → receiver
jira_webhook.handler
  → NOT a dispatch (bot-loop guard) → notify_jira_agent_activity():
     targets = issue reporter + assignee + users @mentioned in the agent's comment
     for each: identity_map lookup by jira accountId → notif_pref row?
       pref opted-in to "agent_commented"/"agent_replied" →
         notify.notify_user(identity_id, tier, event, text)  → Slack DM
           (conversations.open with the identity's slack handle; degrade to silence
            if no verified slack handle — never mis-ping)
  → channel fan-out: notify.notify(tier="actionable"|"informative", event, repo=None,
     project="ENG", unit=issue_key) → subscribed channels (§11.2)
```

### 3.4 Reject path

Identical discipline to Slack §3.2: a non-ALLOW decision records a `blocked_authz` assignment, emits `TriggerDenied` `{source:"jira", agent, site, reason}`, and posts a **specific** reason to the issue:

> ⛔ You aren't authorized to trigger `@workitems` on this project. Reason: **project not onboarded**. Ask an admin, or check assignment `<id>` in the dashboard.

A first-touch (pending-identity) user gets the standard onboarding reply — email promise included, since Jira reliably supplies a verified email for most sites (org-copy variant from spec §16.4).

---

## 4. Data model (`infra/dashboard/config_store.py`)

New record kinds in `fleet-config-${Stage}`, following the `pk`-prefix + `kind-index` GSI convention. All ids validated at the store boundary (Cedar-metachar safe): site id `^[0-9a-f-]{36}$` (Atlassian cloud id) or `^[a-z0-9-]+$` (site slug), project key `^[A-Z][A-Z0-9]{1,9}$`, account id `^[0-9a-z:-]{1,128}$`.

### 4.1 Jira site — `pk="jira_site#<site_id>"`

```jsonc
{
  kind: "jira_site",
  site_id: "<cloud_id>",              // Atlassian cloud id (uuid)
  site_url: "https://acme.atlassian.net",
  site_name: "Acme",
  enabled: true,
  auth_mode: "api_token",             // "api_token" (recommended, §5.2) | "oauth"
  bot_account_id: "712020:abc…",      // the fleet service account — mention anchor + loop guard
  bot_email: "sdlc-agents@acme.com",
  api_token_param: "/sdlc-agents/<stage>/jira/<site_id>/api-token",  // SSM SecureString
  forge_app_id: "ari:cloud:ecosystem::app/…",   // pinned at install verification (§6.1) — no webhook secret exists
  webhook_last_seen_at: <epoch|null>,           // liveness, stamped by the receiver (§12 Verify delivery)
  token_expires_at: <epoch|null>,     // API tokens expire (max 1 yr) → credential_expired notify
  default_project_policy: "allowlist" | "denylist",   // WHERE posture, mirrors Slack channels
  onboarded_by, onboarded_at,
  status: "pending" | "active" | "disabled"
}
```

The API token per site, SecureString, fetched per-invocation (T-8/T-36); there is no webhook secret (Forge transport, §6.1). Multi-site = multiple rows; the receiver selects the row by the `{site}` path parameter (baked into each Forge installation's environment) and cross-checks it against the delivery's cloud id.

### 4.2 Project policy — `pk="jira_proj#<site_id>#<KEY>"`

The WHERE axis, exactly parallel to `slack_chan#` rows:

```jsonc
{
  kind: "jira_project",
  site_id, project_key: "ENG",
  project_name: "Engineering",
  mode: "allow" | "deny",
  repos: ["owner/repo", …],           // the project's direct-work repo scope (mirrors slack_channel.repos)
  note, created_by, created_at
}
```

Interpreted against the site's `default_project_policy` (allowlist recommended for production). `trigger_grants.channel_allowed` generalizes: for `source=="jira"` the "channel" slot carries the project key, so **Cedar policy P3 needs no change** — `context.channelAllowed` is simply the project posture boolean.

### 4.3 Automation rule — `pk="automation_rule#<uuid>"` (§10)

```jsonc
{
  kind: "automation_rule",
  rule_id: "<uuid>",
  connector: "jira",                  // schema is source-agnostic; jira first
  enabled: true,
  event: "issue_transitioned" | "issue_created" | "issue_commented" | "issue_assigned",
  match: {                            // ALL present keys must match (AND); values exact or "*"
    site: "<site_id>" | "*",
    project: "ENG" | "*",
    to_status: "Code Review",         // issue_transitioned only
    from_status: "*",
    issue_type: "*",
    labels_any: ["needs-review"]      // optional OR-set
  },
  action: {
    agent_id: "adr",
    instruction_template: "Review the code for {{issue_key}}: {{summary}}. PRs are linked on the issue."
  },
  cooldown_seconds: 3600,             // per (rule, issue) dedup window — loop/flap brake
  created_by, created_at, updated_at,
  last_fired_at, fire_count           // observability
}
```

Template variables (allowlisted, HTML-escaped-none/plain-text): `{{issue_key}} {{summary}} {{project}} {{status}} {{from_status}} {{to_status}} {{issue_type}} {{reporter}} {{assignee}} {{site_url}}`. Unknown variables render empty (never raw user text injection beyond `summary` — which the guardrail scans like any other instruction).

### 4.4 Per-user notification preference — `pk="notif_pref#<identity_id>"` (§11.3)

```jsonc
{
  kind: "notif_pref",
  identity_id: "<uuid>",              // the cross-source identity (spec §16) — NOT a slack uid
  dm_enabled: true,
  slack_team: "T0ACME",               // which workspace to DM in (must have a verified slack handle there)
  events: ["agent_commented", "agent_replied", "run_completed", "run_failed", "awaiting_approval"],
  min_tier: "informative" | "actionable" | "error",
  created_at, updated_at
}
```

### 4.5 Extensions to existing kinds

- `trigger_rule.connector` gains `"jira"` (`TRIGGER_CONNECTORS = ("slack","asana","github","jira")`).
- `identity.handles` gains `jira: "<accountId>"` (+ `handle_keys` entries `jira:<accountId>`); `IDENTITY_SOURCES` gains `"jira"`.
- `notif_sub` (channel subscriptions) gains an optional `projects: ["ENG", …]` scope alongside `repos`, validated ⊆ onboarded projects (the §18.2/T-43 discipline, second axis).

### 4.6 New `config_store` functions

Mirroring the existing style, each paged + id-validated:
`list_jira_sites / get_jira_site / put_jira_site / set_jira_site_status / delete_jira_site`; `list_jira_projects(site_id) / put_jira_project / delete_jira_project`; `list_automation_rules(connector=None) / get_automation_rule / put_automation_rule / set_automation_rule_enabled / delete_automation_rule`; `get_notif_pref(identity_id) / put_notif_pref / delete_notif_pref`.

---

## 5. Identity, credentials, and trigger authorization

### 5.1 Principals

- **Human:** `jira:<accountId>` — Atlassian account ids are immutable and global across sites (the T-4 rationale; never the display name). Applied centrally in `router.namespaced_principal`.
- **Automation:** `automation:jira:<rule_id>` — a synthetic, per-rule principal (§10.4). Distinct namespace so a rule can never be confused with, or inherit grants from, a person.
- **Identity enrichment:** the webhook's `comment.author` / `user` object supplies `accountId`, `displayName`, and (when the site's privacy settings allow) `emailAddress` — sourced from Atlassian's authenticated directory, so it seeds the identity map as **verified** (`email_verified=True`), joining the person to their GitHub/Slack/Asana handles (T-42 discipline). When email is hidden by privacy settings, the identity is created email-less and merges later via admin review or another source's verified email — the standard §16.5 path.

### 5.2 Site credentials — service account + API token (recommended), OAuth 3LO (alternative)

**Recommended: a dedicated Jira service account** (e.g. `sdlc-agents@acme.com`, display name "SDLC Agents") **with an API token** (Basic auth to REST v3):

- **It gives the fleet a mention anchor.** With OAuth 3LO the API acts *as the authorizing admin* — there is no bot to `@mention` and every agent comment would impersonate that admin. A service account makes `@sdlc-agents` real in Jira's mention picker and makes bot-loop filtering exact (`comment.author.accountId == bot_account_id`).
- **It matches the fleet's easiest onboarding UX** — paste-a-token, exactly like Slack's one-step workspace connect. No OAuth callback surface on the dashboard, no rotating-refresh-token persistence problem (Atlassian 3LO refresh tokens rotate on every use and revoke the whole token family on reuse — a serialization hazard for concurrent Lambdas).
- **Scoped API tokens** (Atlassian now supports scoping) SHOULD be used, scoped to Jira read/write work; classic unscoped tokens are accepted but flagged in the UI.
- **Expiry is managed, not ignored:** `token_expires_at` is captured at connect; a scheduled check emits the existing `credential_expired` error-tier notification 14/3/0 days out, and the connector page shows a countdown badge.

**Alternative (kept, not default):** OAuth 3LO via `scripts/bootstrap_jira_oauth.py` for orgs that forbid service accounts. `auth_mode: "oauth"` sites store client id/secret/refresh-token at the already-defined SSM paths; the broker then serializes token refresh through a conditional-write lock row (`jira_token#<site_id>` in the config table) to survive rotation. This path is documented but **not** built in Phase 1–3 (§16).

Comment attribution: all agent comments post as the service account; each carries the agent's signature line (`🤖 **[Workitems Agent]** · run <assignment_id>`), matching the Slack per-agent persona convention in spirit (Jira has no `chat.customize` equivalent).

### 5.3 Trigger authorization — zero new Cedar

The AVP `TriggerPolicyStore` fixed 3-policy set is untouched:

- **WHO** — `trigger_rule` rows with `connector:"jira"`; subjects are `jira:<accountId>` principals, permission groups (via the identity map — a group grant authored once already applies to Jira automatically, the §17 payoff), or automation principals `automation:jira:<rule_id>`.
- **WHERE** — `context.channelAllowed` is computed by `trigger_grants.project_allowed(site_id, project_key)` (new sibling of `channel_allowed`, same posture math over `jira_proj#` rows + the site's `default_project_policy`). `context.workspace` carries the `site_id`; `context.channel` carries the project key. Unknown/disabled site ⇒ False (fail-closed).
- **Fail-closed invariants** hold unchanged: unresolved sender rejected pre-AVP, grant-read failure ⇒ `authz-unavailable`, deny ⇒ `blocked_authz` row + `TriggerDenied` metric + threaded reason (§3.4).

---

## 6. Jira receiver (`infra/dispatch/jira_webhook.py`, new)

A thin adapter mirroring `github_webhook.py`, `jira-webhook-<Stage>` Lambda, route `POST /jira/webhook/{site}` on the shared `WebhookApi`. Always deployed, inert until a site row exists (Slack §19 posture).

### 6.1 Event transport — the shared `atlassian-events` Forge forwarder

Events arrive via the **shared Forge forwarder app** (`forge/atlassian-events/`), fully specified in `confluence-connector-spec.md` §6.1 — one Forge app, installed once per Atlassian site via a private installation link, covering **both** Jira and Confluence. The Jira-specific pieces:

- **Product triggers subscribed** (the Jira module of the shared manifest): comment created, issue created, issue updated (`avi:jira:*` event set). Nothing else in v1. Forwarded to `POST /jira/webhook/{site}`.
- **Delivery verification**: every forwarded call carries a **Forge Invocation Token** verified RS256 against Atlassian's published JWKS (signature, expiry, audience, pinned `forge_app_id`) via the shared `mentions.verify_forge_invocation_token(...)` helper — **no per-site webhook secret exists**. Bad/missing/expired token ⇒ 401; JWKS unreachable ⇒ 503 (fail closed, Forge retries).
- **What this deletes from the original draft**: the admin-registered System WebHook, its copy-pasted secret, the `webhook_secret_param` SecureString, and the HMAC verification path. Onboarding step 3 becomes "install the app" (§15). The original draft's rejected alternative — OAuth-app *dynamic* webhooks with their 30-day expiry (silent-deafness hazard, T-47) — stays rejected; the Forge app's triggers are declarative manifest state and never expire, which is an even stronger answer to T-47 than static webhooks were.
- **Shared-app coupling (accepted)**: a manifest change for either product prompts admin upgrade-consent on every installed site; uninstalling the app silences both connectors on that site — surfaced by each connector page's `webhook_last_seen_at` liveness (§12). Deploy via `scripts/deploy_forge_atlassian.py` (once, by fleet operators; amortized across both connectors).

### 6.2 Correctness requirements (the parts a naive port gets wrong)

- **Site binding by path.** The `{site}` path parameter (baked into the Forge installation's environment) selects the site row. Belt-and-braces: cross-check the FIT's installation cloud id and the payload's `issue.self` host against the row; mismatch ⇒ drop + metric (an install on the wrong site must not be processed under another site's policy — T-37 analogue).
- **Bot-loop prevention.** Drop any event whose actor (`comment.author.accountId` / `user.accountId`) equals the site's `bot_account_id` **before** mention/automation matching — except that agent-authored `comment_created` events feed the notification path (§3.3) only. An automation rule can therefore never be triggered by an agent's own write to the same issue, and a mention inside an agent's comment never dispatches (T-34/T-49).
- **Dedup.** Forge retries failed deliveries. Dedup on a TTL'd `jira-event#<sha256(eventType + issue.id + comment.id|changelog.id + timestamp)>` item in the assignments table (the `slack_event_dedup` shape) — check-before / mark-after-success, fail-open on store errors (duplicate dispatch tolerated; dropped mention not).
- **Mention detection — ADF first, text fallback.** Jira comments arrive as Atlassian Document Format. Scan for a `{"type":"mention","attrs":{"id": <bot_account_id>}}` node; the agent id is the first registry-resolvable token *after* the mention node in the flattened text (Slack's `@sdlc-agents workitems …` pattern). Fallback for raw-text bodies: `mentions.resolve_mention` on the flattened text (`@workitems` form). Flattening is a small ADF→text walker (`_adf_to_text`) — do not regex the JSON.
- **Context front-loading (GitHub-style).** Build `source_context` with the issue snapshot + **all comments** flattened as `"[<displayName> at <iso>]:\n<text>"` blocks (one REST call, `GET /rest/api/3/issue/{key}?fields=…&expand=renderedFields` + `GET …/comment`), best-effort — a fetch failure still dispatches base context `{site, project_key, issue_key}`. This is what makes multi-turn conversation on an issue work: every mention re-dispatches with the full thread.
- **Automation matching runs only when no mention resolved** (a mention is always the user's explicit intent and wins), and only for events an enabled rule subscribes to (§10.2).
- **3-second-style ack.** Jira's timeout is more lenient than Slack's, but the same discipline applies: verify → dedup → async `Event`-invoke → 200. The "🏁 on it" ack is posted by the **router** (`_post_block_reply` sibling), never on the receiver's request path.
- **No `ssm:PutParameter`** on the receiver role — secrets are written by the admin connect flow only (T-9 discipline).

---

## 7. Outbound replies & agent conversation

- **`reply.post_jira_comment(site_id, issue_key, body) -> bool`** — fetch the site's API token per-invocation, `POST /rest/api/3/issue/{key}/comment` with the body wrapped in minimal ADF (`_text_to_adf`); returns bool, non-fatal on failure + metric (the existing `post_github_comment` contract). Used by the router for acks, guardrail blocks, authz rejects, and onboarding replies.
- **`router._post_block_reply`** gains a `jira` branch; the router posts the success ack (like Slack).
- **Agent result round-trip:** agents post their final answer as an ordinary gateway tool call — `JiraTarget___add_comment` — steered by the dispatch block's `Reply to:` line (`agents/shared/dispatch_context.py` gains `jira_dispatch_block()` rendering site/project/issue/status/comment history + the project's approved repo scope). This preserves the gateway-only chokepoint; no dispatch-side posting of results for Jira (unlike Slack, agents *can* hold a Jira write path — through the gateway, Cedar-scoped).
- **Conversation:** each follow-up mention is a fresh dispatch carrying the full comment history (§6.1), so the agent sees its own prior replies and the user's feedback — the same propose → human-approve → execute loop workitems runs on Asana works verbatim on a Jira issue.

---

## 8. Gateway target: `JiraTarget` + broker + Cedar

### 8.1 Why a Lambda broker (GitHub-style), not the Atlassian remote MCP

- **Curated tool surface.** An `InlinePayload` schema means destructive tools (`delete_issue`, `delete_comment`, `delete_worklog`) are **structurally absent** — the strongest guarantee, same as GitHub's `DESTRUCTIVE_TOOLS` posture. The remote MCP's tool set is Atlassian-defined and `ListingMode: DYNAMIC`, which also trips the documented DYNAMIC-listing/policy-write trap (template lines ~2811–2831) and requires the OAuth credential provider.
- **Project scoping the credential can't express.** A Jira API token is **site-wide** — unlike GitHub App installation tokens there is no per-project credential. The broker is therefore the enforcement point: every tool call is validated against the onboarded-project allowlist before any Jira call (defense in depth under Cedar).
- **Per-agent least privilege** intersected per tool, mirroring `scm_broker._TOOL_PERMISSIONS` ∩ `AGENT_GITHUB_PERMISSIONS`.

`infra/dispatch/jira_broker.py` (`jira-broker-<Stage>`): dispatches on `bedrockAgentCoreToolName` client context; reads the trusted `_dispatch_agent`/`_dispatch_origin` args injected by the interceptor; derives `project_key` from the `issue_key` prefix and **requires arg-consistency** (an explicit `project_key` arg must match the issue key's prefix); checks the project allowlist + the agent's Jira tier; then calls REST v3 with the site token. Fails closed on missing agent tier or unknown site/project.

The **interceptor** (`scm_interceptor.py`) gains a Jira clause: for `JiraTarget___*` calls it validates the project against the *dispatch origin's* co-scope — a Jira-originated dispatch may act on its own project (+ the project's `repos` for GitHub tools); a GitHub-originated dispatch may act on Jira projects linked to that repo (the `jira_proj.repos` edge, read in reverse). Same rationale as co-repo grouping: the runtime role is identical across dispatches, so origin pinning can't live in Cedar.

### 8.2 Curated tool set (v1)

| Tool | Class | Args (required) |
|---|---|---|
| `get_issue` | read | site, issue_key |
| `get_issue_comments` | read | site, issue_key |
| `search_issues` | read | site, jql, max_results≤50 |
| `list_projects` | read | site |
| `get_project` | read | site, project_key |
| `get_transitions` | read | site, issue_key |
| `add_comment` | write | site, issue_key, project_key, body |
| `create_issue` | write | site, project_key, issue_type, summary (+description, labels) |
| `update_issue` | write | site, issue_key, project_key, fields{summary/description/labels/priority} |
| `transition_issue` | write | site, issue_key, project_key, transition_name (+resolution) |
| `assign_issue` | write | site, issue_key, project_key, account_id |
| `link_issues` | write | site, inward_key, outward_key, project_key, link_type |
| `add_remote_link` | write | site, issue_key, project_key, url, title |

Absent by construction: any delete, worklog writes, sprint/board writes, project/user admin. `jira_broker.tool_definitions()` generates the schema; a pin-test keeps the template copy in sync (the `scm_broker` regeneration pattern); `scripts/check_gateway_manifest.py` coverage extends to `JiraTarget` before ENFORCE.

### 8.3 Cedar grants — who may change tickets

`infra/dashboard/fleet_policy.py`: add `JIRA_TARGET = "JiraTarget"`, `JIRA_TOOL_CLASS` (the table above), extend `classify_tool()`/`tool_catalog()` (so dashboard-authored custom agents can be granted Jira tools per class), and extend `AGENT_TOOL_GRANTS`:

| Agent | Jira grants |
|---|---|
| `workitems` | all reads + `add_comment, create_issue, update_issue, transition_issue, assign_issue, link_issues, add_remote_link` — the PM owns ticket state |
| `adr` | all reads + `add_comment, add_remote_link` |
| `docwriter` | all reads + `add_comment, add_remote_link` |
| `researcher` | reads only |

Mirror intent in `cedar/jira.cedar` (advisory) + a `shared.cedar` forbid row for the delete family (documentation of intent; they're structurally absent anyway). Fleet forbid `sdlc_allowed_jira_projects`: `JiraTarget` write tools forbidden `unless { context.input.project_key == "ENG" || … }`, rendered from the onboarded-project rows exactly like `sdlc_allowed_repos`. `policy_sync._available_target_names()` already tolerates granting before the target deploys — grants can merge ahead of the `DeployJiraTarget` flip.

Prompt updates: `workitems/prompts.py` et al. gain Jira terminology + the hard rules ("NEVER transition an issue to Done/Closed without explicit human approval on the issue"; the signature line; honest-error-reporting applies).

---

## 9. Traceability

### 9.1 Run → issue (dispatch side)

`enrichment.derive_trace_refs` gains a native branch: `source == "jira"` emits `jira_key`, `jira_project`, `jira_site` (+ the regex hint keeps working for the other sources — a GitHub PR mentioning `ENG-142` still joins). `derive_participants` emits the reporter/assignee/commenter with `source:"jira"`. The dashboard needs **no schema change** (`trace_refs` is an open map; `jira_key` is already in `queries.TRACE_DIMENSIONS`; the FleetView source filter is data-driven). `format.sourceLink` gains the `https://<site>/browse/<KEY>` branch so the trace chip is clickable.

### 9.2 Issue → work (Jira side)

- Agents stamp **remote links** on the issue for every artifact they produce (`add_remote_link`: PR URL, created GitHub issue, dashboard run deep-link `https://<dashboard>/#/runs/<assignment_id>`), steered by prompt rules + the `post_results` prompt-tool pattern.
- `agents/workitems/tools/sync.py` conventions extend: GitHub issues created from a Jira issue carry the Jira key in the title/body (`[ENG-142] …`) and a `tracked-in-jira` label; the enrichment regex then closes the loop automatically on later GitHub-side runs.
- Runtime enrichment (`agents/shared/assignment.py::update_trace_refs`) is unchanged — Jira-originated runs that produce PRs get `pr_url`/`branch` merged onto the same assignment, so one trace query shows issue → run → PR.

---

## 10. Event automation rules (new engine)

### 10.1 What it is

A data-driven **event → agent** rule engine: `automation_rule#` rows (§4.3) matched by receivers against normalized event facts, dispatching through the **unmodified** router spine. Today's nearest precedents (Asana bot-assignment env maps, `_notify_scm`'s event mapping) are hardcoded; this replaces "edit env vars" with "author a rule in the dashboard". The schema is source-agnostic (`connector` field) so GitHub/Asana rules ("PR opened → docwriter drafts release notes") are a follow-up with zero schema work — Jira ships first.

### 10.2 Matching (`infra/dispatch/automation.py`, new)

`automation.match(connector, event, facts) -> list[Rule]` — reads enabled rules via the standard `kind-index` + 30s TTL cache (the `trigger_grants` pattern), applies the `match` block (AND over present keys; `"*"` wildcards; `labels_any` OR-set). Called by the receiver only when no mention resolved (§6.1). Per-rule **cooldown dedup**: a TTL'd `auto-fire#<rule_id>#<issue_key>#<to_status>` item (assignments-table shape) suppresses re-fires inside `cooldown_seconds` — the brake against status flapping and edit-storms.

### 10.3 Dispatch semantics

Rendered instruction (allowlisted variables, §4.3) → normal dispatch payload with `trigger_type: "automation"`, `sender: "automation:jira:<rule_id>"`, `agent_id` pre-resolved. Everything downstream is stock: identity gate is bypassed **only** in the sense that automation principals are machine identities — they are *not* auto-usable; they authorize via §10.4. Guardrail scans the rendered instruction (issue summaries are user text — T-1 applies). Concurrency caps apply per agent. The assignment records `trace_refs.automation_rule_id`, so the dashboard can answer "what did this rule run?" (`/trace?dim=automation_rule_id`).

### 10.4 Authorization — rules are grants, not bypasses

Creating/enabling a rule is **admin-gated** (`is_admin`, like all connector writes). On create, the admin API auto-authors a **permit `trigger_rule`** `{connector:"jira", subject_type:"user", subject_id:"automation:jira:<rule_id>", agent_id:<rule's agent>, workspace:<site|*>}` with a deterministic rule id (the channel-approval overwrite pattern); on delete/disable it removes it. The router therefore evaluates automation dispatches through the same AVP path — deleting the grant kills the rule's power even if a stale cache still matches it (default-deny backstop). The WHERE axis also applies: a rule on a non-onboarded project is dead on arrival (P3 forbid).

### 10.5 Loop safety (T-49)

Three independent brakes: (1) actor guard — events by `bot_account_id` never match rules (§6.1); (2) cooldown dedup (§10.2); (3) a per-rule hourly fire ceiling (`AUTOMATION_MAX_FIRES_PER_HOUR`, default 20) with an `AutomationRuleThrottled` metric + error-tier notification. An agent transitioning an issue *can* legitimately fire a rule for a *different* agent (that's a feature — pipelines), but the chain is bounded: automation-triggered runs carry `trigger_type:"automation"`, and a rule may not be matched by an event whose assignment chain already contains that rule id (chain-id passed in dispatch context, checked at match time; depth cap 3).

### 10.6 UX

Authored on the Jira connector page's **Automations** tab (§12): pickers for event/site/project/status (statuses fetched live from the site), agent dropdown (registry), template editor with variable chips + live preview, enable toggle, per-rule activity (last fired, fire count, recent runs deep-link). The example from the requirements is literally the first template: *"If an issue moves to 'Code Review' start agent `adr` to review the code for the issue."*

---

## 11. Notifications

### 11.1 New events in the catalog (`slack_notify.TIER_EVENTS`)

- **actionable:** `agent_replied` (an agent replied to/mentioned *a person* on an issue) — mentions the person (§18.4 rules apply).
- **informative:** `agent_commented` (an agent posted on a subscribed project's issue), `issue_transitioned`, `automation_fired`.
- **error:** `automation_throttled`, plus the existing `credential_expired` now fired by the Jira token-expiry check (§5.2).

### 11.2 Channel subscriptions (existing system, extended)

`notif_sub` rows gain the optional `projects` scope (§4.5); `notify.notify()` gains a `project=` match axis alongside `repo`. Emitters: the Jira receiver (agent-comment + transition events, §3.3), the automation engine (`automation_fired`/`automation_throttled`), the token-expiry check. Threading: `unit = issue_key`, so an issue's whole lifecycle collapses into one thread per channel — the §18.4 anti-spam contract. The `/sdlc-notify` modal gains a project multi-select (same 75-char index-value trick as repos).

### 11.3 Per-user Slack DMs (new surface)

The requirement: *"if an agent comments on an issue or replies to a user, they should be able to configure to receive Slack notifications."* Today notifications are channel-scoped only; this adds the per-user tier on top of the identity map:

- **Opt-in, self-serve:** `/sdlc-notify me` (subcommand of the existing slash command) opens a personal modal → tier/event checkboxes → writes `notif_pref#<identity_id>` (§4.4). The submitting Slack user resolves through `identity.resolve` — only an **active** identity with a **verified** Slack handle may hold prefs (T-52). A **Notifications** section on the dashboard **Access → Users** panel gives the same read/edit to admins and to the user's own row.
- **Delivery:** `notify.notify_user(identity_id, tier, event, text, unit)` — `conversations.open` (`im:write` scope, manifest update) with the identity's Slack handle for their chosen `slack_team`, then `chat.postMessage` threaded per unit. No verified handle for that team ⇒ **silent degrade** (metric, never a mis-ping) — the §18.4 rule.
- **Targeting for Jira events (§3.3):** `agent_commented` targets the issue's reporter + assignee; `agent_replied` targets users the agent's comment @mentions (ADF mention nodes) — each resolved Jira accountId → identity → pref check → DM. The same seam serves non-Jira events for free: `run_completed`/`run_failed`/`awaiting_approval` DMs to the run's requester come from `assignment_notifier.py` calling `notify_user` on status transitions — one new call site.
- **Anti-spam:** DMs honor `min_tier`; an event that already mentioned the person in a subscribed channel does not also DM them (dedup key `(identity, event, unit)` short-TTL); informative-tier DMs are digest-batched per issue thread (thread-reuse via the `notif_thread#` mechanism keyed per identity DM channel).

---

## 12. Connectors UI — `dashboard/src/connectors/JiraConnectorPage.tsx`

Registry entry in `registry.ts` (`id: "jira"`, health badge from `connectorStatus("jira")` = site count + token-expiry warnings + webhook liveness). Route `#/connectors/jira`. Tabs (the Slack page is the chrome template):

- **Sites** — the guided connect (§15): create-service-account checklist → paste site URL + service-account email + API token → **Connect** (one POST verifies `GET /rest/api/3/myself`, resolves `bot_account_id` + cloud id, stores the SecureString, writes the row) → **app-install card** showing the shared `atlassian-events` Forge app's private installation link + its manifest scopes/egress (already installed for Confluence? the card shows "installed" immediately), with a "Verify delivery" button (liveness via the site row's `webhook_last_seen_at`, stamped by the receiver). Per-site enable/disable/remove, token-expiry countdown badge.
- **Projects** — allow/deny rows + posture toggle + per-project repo scope (mirrors the Channels tab; project list fetched live from the site).
- **Access rules** — `TriggerRulesPanel` filtered to `connector="jira"` + the **Test access** simulator (subject × agent × site × project → ALLOW/DENY + deciding policy).
- **Automations** — §10.6.
- **Notifications** — channel subs with project scope (admin view), link to per-user prefs on Access → Users.
- **Activity** — `ActivityPanel source="jira"` + receiver errors + `TriggerDenied` + `AutomationRuleThrottled`.

`types.ts` additions: `JiraSite`, `JiraProject`, `AutomationRule`, `NotifPref`; `TriggerRule.connector` union += `"jira"`. `api.ts`: `listJiraSites/connectJiraSite/deleteJiraSite`, `listJiraProjects/putJiraProject/deleteJiraProject`, `listAutomationRules/createAutomationRule/updateAutomationRule/deleteAutomationRule`, `getNotifPref/putNotifPref`.

---

## 13. Admin API routes (`infra/dashboard/admin.py`)

All `auth.is_admin`, fail-closed, `_route` pattern:

| Method + path | Purpose |
|---|---|
| `GET/POST /admin/jira/sites`, `POST /admin/jira/sites/connect`, `DELETE …/{site_id}` | Site CRUD; `connect` = verify token (`/myself`) + resolve cloud id/bot account + store the SecureString + write row (the Slack one-step-connect pattern) |
| `POST /admin/jira/sites/{site_id}/verify-webhook` | Delivery liveness check for the connect flow (reads the receiver-stamped `webhook_last_seen_at`) |
| `GET/POST /admin/jira/projects`, `DELETE …/{site_id}/{key}` | Project allow/deny + repo scope |
| `GET/POST /admin/automation-rules?connector=`, `PUT/DELETE …/{rule_id}`, `POST …/{rule_id}/enable|disable` | Rule CRUD; create/enable auto-authors the automation `trigger_rule` grant, delete/disable removes it (§10.4) |
| `GET/PUT/DELETE /admin/notif-prefs/{identity_id}` | Per-user DM prefs (admin + self) |
| *(existing)* `/admin/trigger-rules?connector=jira`, `/admin/trigger-rules/simulate` | WHO rules + simulator — connector param only |

The admin Lambda's connect route needs `ssm:PutParameter` on `/sdlc-agents/${Stage}/jira/*` (SecureString write for the API token — the only secret; the Forge transport has no webhook secret) — the one deliberate deviation from the Slack §10 posture, justified because the paste-token UX *is* the first-class onboarding; scoped to the jira path prefix only. No AVP permissions (grants are data; simulator stays local).

---

## 14. Infrastructure (`infra/foundation/template.yaml`)

- **`JiraWebhookFunction`** (`jira-webhook-<Stage>`, mirrors `SlackWebhookFunction`): route `POST /jira/webhook/{site}`; env `REGISTRY_PARAM`, `DISPATCH_FUNCTION`, `FLEET_CONFIG_TABLE`, `ASSIGNMENTS_TABLE` (dedup/cooldown items), `FORGE_JWKS_URL`; SSM **read** on `/sdlc-agents/${Stage}/jira/*` (API token, for context fetches); no `ssm:PutParameter`; outbound HTTPS to Atlassian's JWKS endpoint; `lambda:InvokeFunction` on the router. Always deployed, inert-until-onboarded.
- **Forge forwarder app** (`forge/atlassian-events/`, shared with the Confluence connector — see confluence-connector-spec §6.1/§15): the Jira module + `avi:jira:*` triggers land in the same manifest; deployed via `scripts/deploy_forge_atlassian.py`, outside the SAM stack.
- **`JiraBrokerFunction`** (`jira-broker-<Stage>`, mirrors `ScmBrokerFunction`): SSM read on the jira path; config-table read (site/project rows). No `PutParameter`.
- **`JiraGatewayTarget`** (`AWS::BedrockAgentCore::GatewayTarget`): `Name: JiraTarget`, `CredentialProviderConfigurations: [GATEWAY_IAM_ROLE]`, `TargetConfiguration.Mcp.Lambda` → broker ARN + `ToolSchema.InlinePayload` (§8.2). Gated `DeployJiraTarget` (default false) only because the gateway itself is `DeployGateway`-gated; flip after grants merge. `FleetGatewayRole` gains invoke on the broker.
- **Interceptor**: Jira clause (§8.1) — same function, no new resource.
- **Token-expiry check**: fold into the existing weekly scheduled Lambda (`capability_rebuilder`'s schedule pattern) or a small `jira-token-check` scheduled rule → `credential_expired` notify.
- **Alarms**: `jira-webhook-errors-<Stage>`, `jira-broker-errors-<Stage>`, `AutomationRuleThrottled` alarm.
- **Outputs**: `JiraWebhookEndpoint` (per-site URL is endpoint + `/<site_id>`).
- Router IAM/env: unchanged (trigger store already wired).

---

## 15. First-class onboarding UX

**Skill:** `skills/sdlc-agents-connect-jira/SKILL.md` (template: `sdlc-agents-connect-asana`), and `skills/sdlc-agents/SKILL.md` Step-1 toolchain discovery flips Jira from "planned, be honest" to a real connect path; `sdlc-agents-select` `pm: jira` becomes valid. `docs/aws-deploy.md` gains the §2.4 SSM rows + §3 steps.

**Admin walk-through (the whole thing, ~10 minutes):**

1. In Jira: create the `sdlc-agents` service account (checklist rendered in the UI: name it "SDLC Agents", grant it the target projects' work permissions), sign in as it once, mint an API token (scoped, 1-yr max).
2. Dashboard → Connectors → Jira → **Connect a site**: paste site URL + service-account email + token → Connect (verifies + stores + onboards in one POST).
3. The page shows the **app-install card**: install the shared `atlassian-events` Forge app from its private installation link (scopes + egress shown up front; if the Confluence connector already installed it on this site, the card shows "installed" and this step is a no-op) → click **Verify delivery** → green check.
4. **Projects tab**: onboard `ENG` (allowlist), attach its repo scope.
5. **Access**: grant a permission group (or users) the WHO rules; simulator to confirm; existing group grants already apply (identity-map payoff).
6. Users `@sdlc-agents workitems …` on an issue. First-touch users get the standard onboarding reply → admin approves in Access → Users.
7. Optional: author the first automation rule from a template; users run `/sdlc-notify me` for DMs.

---

## 16. Delivery phases (the plan)

Each phase is independently shippable and test-gated; enums/branches from §2's checklist land with the phase that first needs them.

1. **Dispatch spine** — the shared `forge/atlassian-events/` app (Jira module; build the app + `mentions.verify_forge_invocation_token` here if Confluence hasn't already — otherwise extend the manifest), `jira_webhook.py` (FIT verification/dedup/bot-loop/mention/ADF walker/context front-load), `reply.post_jira_comment`, router `jira` branches (`namespaced_principal`, `_post_block_reply`), `identity`/`config_store` enums + `jira_site#`/`jira_proj#` rows + store functions, `trigger_grants.project_allowed`, `enrichment` native branch, `assignment_notifier._actor_from`, template: webhook fn + routes + alarm. *Exit: a mention on an onboarded site/project dispatches an agent; rejects/onboarding replies land on the issue; existing suites green.*
2. **Agent tools** — `jira_broker.py` + `JiraGatewayTarget` + interceptor clause, `fleet_policy.py` (`JIRA_TOOL_CLASS`, grants, project forbid), `cedar/jira.cedar`, `check_gateway_manifest.py`, `jira_dispatch_block()`, prompt updates, delete `agents/workitems/tools/jira_mcp.py`. Roll out LOG_ONLY → ENFORCE (existing `GatewayPolicyEnforcement` path). *Exit: workitems reads + writes tickets; adr/docwriter comment-only; researcher read-only; agents answer on the issue via the gateway.*
3. **Onboarding UI + admin API** — `JiraConnectorPage` (Sites/Projects/Access/Activity tabs), connect + verify-webhook + project routes, `registry.ts`/`types.ts`/`api.ts`/`App.tsx`/`format.sourceLink`, admin SSM write grant (jira path). *Exit: the §15 walk-through works end-to-end with no CLI.*
4. **Automation engine** — `automation.py` (match/cooldown/chain-guard), receiver hook, `automation_rule#` CRUD + grant auto-authoring, Automations tab, throttle metric/alarm/notify. *Exit: the "Code Review → adr" rule fires, is audited, is throttleable, and dies when disabled.*
5. **Notifications** — `TIER_EVENTS` additions, `notif_sub.projects`, receiver notify hooks, `notif_pref#` + `notify_user` DM path (`im:write` manifest bump), `/sdlc-notify me` modal, `assignment_notifier` requester-DM call, dashboard prefs UI, token-expiry check. *Exit: a user opts in and gets a threaded DM when an agent replies to them; channels get project-scoped fan-out; no mis-pings (degrade tested).*
6. **Docs, skills, threat model, rollout** — connect skill + quickstart flip, `aws-deploy.md`, threat-model rows (§18), deploy dev → gamma → prod with `DeployJiraTarget=true` after grant merge.

---

## 17. Testing

Mirrors the per-module style of the Slack suites; every phase lands with its tests.

- **`jira_webhook`**: valid Forge Invocation Token → correct dispatch payload; bad signature / expired / wrong audience / wrong app id → 401; JWKS unreachable → 503 (fail closed); key-rotation (new kid) → refetch and verify; unknown/disabled site → drop; cloud-id / `issue.self` host mismatch → drop+metric; dedup (same delivery twice → one dispatch; fail-open on store error); bot-actor events never dispatch/match rules but do feed notify; ADF mention resolution (+ text fallback, unknown mention → no-op); comment history front-loaded; context fetch failure still dispatches base context; automation matched only when no mention.
- **`jira_broker`**: tool dispatch by name; project derived from issue key + arg-consistency (mismatched `project_key` → error); allowlist enforcement; per-agent tier enforcement (researcher write → denied); unknown site fail-closed; token fetched per-invocation; ADF wrapping of comment bodies; tool_definitions ↔ template pin-test.
- **`fleet_policy`/`policy_sync`**: JIRA grants render; project forbid renders from rows (`unless {false}` when empty); undeployed-target filtering; `classify_tool` on Jira names.
- **`trigger_grants`**: `project_allowed` posture math (allowlist/denylist/unknown-site-closed/non-jira-open); automation principal grant resolution.
- **`automation`**: match semantics (AND keys, wildcards, labels_any); cooldown suppress; hourly ceiling; chain-depth guard; template rendering (allowlisted vars, unknown → empty); disabled rule inert; grant auto-author/remove on create/delete.
- **`notify`/`slack_notify`**: project-scope matching; `notify_user` (opt-in only, verified-handle only, silent degrade, DM-vs-channel dedup, threading per unit); new events in tiers; `/sdlc-notify me` modal round-trip.
- **`config_store`/`admin`**: new-kind CRUD + id validation (Cedar-metachar, bad keys); connect route (myself-verify mock, SecureString writes, idempotent re-connect); webhook-verify round-trip; automation routes incl. grant coupling; notif-pref self-vs-admin authz; `is_admin` gating throughout.
- **`enrichment`/`reply`/router**: native jira trace_refs + participants; `post_jira_comment` bool contract; authz-deny posts issue comment + `blocked_authz` + `TriggerDenied`.
- **SPA**: `#/connectors/jira` routing; registry descriptor unique/routable; `sourceLink` jira branch; `npm run build` green.

---

## 18. Threat-model additions (`docs/threat-model.md`)

New components **C-23 Jira receiver + service account**, **C-24 Jira broker/target**, **C-25 automation-rule engine**. New threats:

| ID | Threat | Mitigation |
|----|--------|-----------|
| T-46 | Jira webhook forgery/spoofing | per-site secret, `X-Hub-Signature` HMAC timing-safe, fail-closed on missing secret; `issue.self` host cross-check (§6.1) |
| T-47 | Silent webhook deafness (expiring dynamic webhooks) | admin-registered static webhooks only; liveness check + last-seen timestamp on the connector page (§6.1, §12) |
| T-48 | Site-wide token blast radius (no per-project credential exists) | broker project-allowlist enforcement + Cedar project forbid + interceptor origin pinning + curated no-delete tool schema (§8) |
| T-49 | Automation loops / event storms | bot-actor guard, per-(rule,issue) cooldown, hourly ceiling + throttle alarm, chain-depth cap (§10.5) |
| T-50 | Automation privilege escalation (a rule as a grant side door) | rules admin-gated; synthetic `automation:jira:<rule_id>` principal authorizes through the same AVP path via an auto-managed grant; default-deny backstop (§10.4) |
| T-51 | Service-account token theft/expiry | SecureString per site, per-invocation fetch (T-8), scoped tokens recommended, expiry tracked + `credential_expired` alerts (§5.2) |
| T-52 | DM mis-ping / notification identity confusion | prefs keyed on identity_id, active + verified-handle required, silent degrade on unresolved, DM/channel dedup (§11.3) |
| T-53 | Instruction injection via automation templates | allowlisted variables only; rendered instruction passes the edge guardrail like any user text (§10.3) |

Plus DF rows for the Jira inbound flow, the broker outbound flow, automation dispatch, and the DM path, and a changelog row.

---

## 19. Open decisions

1. **Assignment-style trigger** (assign the issue to the service account + an "Agent" select field, mirroring Asana's paths) — deferred; mentions + automation cover the use cases without per-site field setup. Revisit on demand.
2. **OAuth 3LO build-out** (§5.2 alternative) — spec'd, not scheduled; needs the token-refresh lock row. Trigger: a customer org that prohibits service accounts.
3. **Agile tools** (boards/sprints, read-only) — additive to the curated schema when an agent needs them.
4. **Automation for GitHub/Asana events** — the engine is source-agnostic by schema; wiring their receivers is a fast-follow after Phase 4 proves the model.
5. **AgentCore Memory for cross-run issue context** — unchanged from the fleet roadmap; comment-history front-loading is sufficient for v1 conversations.

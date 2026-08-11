# Connector: Atlassian (Jira + Confluence)
## One program: first-class Jira and Confluence connectors on a shared Atlassian foundation — dispatch, agent tools, fine-grained access control, traceability, automation, and notifications

> **Status: SHIPPED (Phases 1–6, 2026-08-11).** All six delivery phases are implemented: the shared site/credential model + admin routes, both brokers/gateway targets + Cedar grants, both receivers + the `atlassian-events` Forge forwarder (`forge/atlassian-events/`, deployed by `scripts/deploy_forge_atlassian.py` — run automatically from `deploy_fleet.py`), the Connectors → Atlassian dashboard page (all tabs incl. Automations), the automation engine + notifications (`/sdlc-notify me` DMs, token-expiry check), and the docs/skill/threat-model updates (threat-model §3.14 — the T-46–T-58 rows below shipped renumbered as **T-54–T-66** to avoid colliding with the agent-authoring threats that took T-46–T-48 first). Notable deltas from the text below: the Forge forwarder is **site-agnostic** (cloud id resolved per invocation, one deploy per stage, no per-site re-deploy; `FLEET_SITE_ID` remains an optional pin) and publishes its install link to SSM so the Sites tab renders the install card with no operator hand-off; `deploy_fleet.py --full` sets `DeployJiraTarget`/`DeployConfluenceTarget=true`. Remaining open items are §A16's deferred decisions.
>
> This spec **merges and supersedes** `jira-connector-spec.md` and `confluence-connector-spec.md` (both deleted in favor of this document). The merge is structural, not cosmetic: the two connectors share a Forge event forwarder, a site/credential model, a service account, identity handling, the ADF walker, the automation-rule engine, and half a threat model — so they are specified and delivered as **one effort with a shared foundation (Part A) and two product capabilities (Parts B and C)**.
>
> **What this program delivers:**
>
> 1. **Jira + Confluence as dispatch sources** — `@sdlc-agents <agent> …` mentions on Jira issues and in Confluence page/inline comments, routed through the existing dispatch spine (identity → AVP trigger authz → guardrail → runtime), replies threaded back to the origin (§B1, §C1).
> 2. **Agent access to Atlassian data** — curated `JiraTarget` and `ConfluenceTarget` Lambda brokers on the AgentCore Gateway, Cedar-scoped per agent: every agent can *read* tickets and team documentation for deep context; *specific* agents may update tickets or create/update pages (§B2, §C2).
> 3. **Agent-maintained documentation** — docwriter keeps Confluence current (release notes, API docs, freshness sweeps) under layered write safety: per-space `propose`/`direct` modes, version guards, attribution, structurally-absent deletes (§C3).
> 4. **Fine-grained access control on two axes** — container scoping (Jira projects / Confluence spaces) and per-agent tool-action grants, enforced across six independent layers (§A4).
> 5. **Traceability** — native `jira_key` / `confluence_page` trace refs, remote links and in-content links back to runs and PRs, cross-platform joins in the dashboard trace view (§B3, §C4).
> 6. **Event automation** — a data-driven, source-agnostic rule engine ("issue moved to *Code Review* ⇒ run `adr`"; "page labeled `sdlc-review` ⇒ run `adr`"), dispatching through the full authz/guardrail spine under auditable synthetic principals (§A8).
> 7. **Notifications** — Atlassian events in the channel-subscription catalog plus per-user Slack DM preferences ("DM me when an agent replies to me") (§A9).
> 8. **First-class onboarding** — one Connectors → Atlassian admin page (connect a site once, enable products, onboard projects/spaces, author access rules, test with the simulator) and one `sdlc-agents-connect-atlassian` skill (§A10–§A12).
>
> **Decisions this merge locks in** (previously open or split across the two specs):
> - **Transport:** the shared `atlassian-events` Forge forwarder for both products (decided 2026-07-24; §A5). No admin-registered webhooks, no webhook secrets.
> - **One site record:** a single `atlassian_site#` row per Atlassian site — one service account, one API token, per-product enablement — replacing separate `jira_site#`/`confluence_site#` rows and the "reuse the Jira credential" pointer.
> - **One principal/handle namespace:** `atlassian:<accountId>` for both products (account ids are global across Atlassian; resolves the former Confluence open decision #2). The dispatch `source` still distinguishes `jira` vs `confluence`.
>
> Prior art built on, not duplicated: the Slack connector spec (trigger-authz data model §4–§5, identity/groups/notifications §16–§18 — all BUILT), `scm_broker.py`/`scm_interceptor.py` (the enforced broker/interceptor idiom), `scripts/bootstrap_jira_oauth.py` (kept as the OAuth alternative path), `agents/workitems/tools/jira_mcp.py` (dead pre-gateway direct-MCP helper — **deleted** by this spec), the `jira_key` regex in `enrichment.py` + `queries.TRACE_DIMENSIONS`, and the roadmap item "Jira + GitLab support".

---

# Part A — Shared Atlassian foundation

## A1. Goals & non-goals

### Goals

1. **Both products, one foundation.** One site connect, one Forge app install, one identity join, one automation engine, one admin surface — then two product capabilities on top. Neither product gates the other: each Part-B/Part-C phase is independently shippable.
2. **Deep context for every agent.** Reads (tickets, docs, comments, search) are first-class for all agents, scoped to onboarded containers. Un-onboarded projects/spaces are invisible to every agent — for Confluence this is the read-confidentiality boundary (§C2.4).
3. **Writes are least-privilege and safe.** Per-agent Cedar grants; container allowlists; curated schemas with deletes structurally absent; Confluence adds per-space write modes and version guards.
4. **Tagging triggers work** on both surfaces, plus label/transition automation — always through the unmodified router spine, never a side door.
5. **No regressions.** GitHub/Asana/Slack dispatch and the existing suites keep passing. Receivers are always deployed but inert until a site is onboarded (the Slack §19 posture — fail-closed at runtime, enabled via Admin; no deploy flags).

### Non-goals

- **Server / Data Center.** Cloud only (REST v3/v2, Atlassian account ids).
- **Compass** and other Atlassian products — the forwarder/site model would extend, but nothing is specced.
- **Jira-side automation authoring** (we react to events; we don't create Jira Automation rules), **agile-board writes** (sprint moves, ranking) in v1, **Confluence personal spaces** (`~` keys rejected), **attachments/whiteboards/databases as write targets**, **space/project administration** (structurally absent).
- **Replacing Asana or repo docs.** Jira is an additional PM surface; Confluence is the team/product documentation surface; doc PRs into `docs/` remain docwriter's code-adjacent path.

## A2. Where this fits in the existing architecture

Everything reuses an existing seam; the genuinely new mechanisms are the Forge forwarder (§A5) and the automation-rule engine (§A8).

| Piece | Existing seam reused | New for Atlassian |
|---|---|---|
| Webhook receivers | Thin adapters over `infra/dispatch/mentions.py`, async `Event`-invoke of the router — same as the GitHub/Asana/Slack receivers | `jira_webhook.py` + `confluence_webhook.py`, fed by the shared Forge forwarder (§A5) |
| Trigger authz | AVP `TriggerPolicyStore` fixed 3-policy set; grants as `trigger_rule` data — **zero Cedar changes** | principal prefix `atlassian:<accountId>`; connector enums `"jira"`/`"confluence"`; container WHERE-axis rows (§A7) |
| Identity | `identity.py` get-or-create + first-touch onboarding gate | `handles.atlassian = <accountId>`; verified email from Atlassian's directory when privacy settings allow |
| Agent tools | Gateway Lambda target + broker (`GitHubTarget`/`scm_broker.py` pattern), `AGENT_TOOL_GRANTS` in `fleet_policy.py`, `policy_sync.py` | `JiraTarget` + `jira_broker.py` (§B2); `ConfluenceTarget` + `confluence_broker.py` (§C2) |
| Replies | `reply.py` + `router._post_block_reply` branches | `post_jira_comment(...)`, `post_confluence_comment(...)` |
| Trace refs | `enrichment.derive_trace_refs` open map; `jira_key` regex + `TRACE_DIMENSIONS` already exist | native `jira` and `confluence` branches (§B3, §C4) |
| Automation | *(nearest precedents: Asana assignment maps, `_notify_scm` event mapping — hardcoded)* | **new** `automation_rule#` engine, source-agnostic schema (§A8) |
| Notifications | `notify.py` fan-out, `slack_notify.py` modal, `notif_sub#` rows, `assignment_notifier.py` | Atlassian events in `TIER_EVENTS`; `projects`/`spaces` sub scopes; per-user `notif_pref#` DM rows (§A9) |
| Admin UI | `connectors/registry.ts` descriptor + page; `TriggerRulesPanel`, `ActivityPanel`, simulator | one `AtlassianConnectorPage.tsx` (§A10) |
| Admin API | `admin.py` `_route` + `config_store` kinds via `kind-index` GSI | `/admin/atlassian/*`, `/admin/automation-rules*`, `/admin/notif-prefs*` (§A11) |

**Enum/branch points that must learn the new sources** (the exhaustive checklist from code survey): `config_store.TRIGGER_CONNECTORS` (+`"jira"`, `"confluence"`) + `IDENTITY_SOURCES` (+`"atlassian"`, mirrored in `identity.py`), `router.namespaced_principal` (both sources → `atlassian:<accountId>`) + `_identity_source_and_handle`, `router._post_block_reply` (two branches), `enrichment.derive_trace_refs`/`derive_participants` (two branches), `assignment_notifier._actor_from`, `reply.py`, `agents/shared/dispatch_context.py` (`jira_dispatch_block`, `confluence_dispatch_block`) + the `agents/_base/agent.py` source switch, `dashboard/src/types.ts` (`TriggerRule.connector`, `Identity.handles`), `connectors/registry.ts` (id union + entry), `format.ts::sourceLink` (`…/browse/<KEY>` and `…/wiki/spaces/<KEY>/pages/<id>` branches), `App.tsx` (route `#/admin/connectors/atlassian`), `infra/foundation/template.yaml` (functions, routes, alarms, targets).

## A3. End-to-end flows (abridged; product specifics in Parts B & C)

```
Mention on a Jira issue / Confluence comment
  ▼  Forge product trigger → atlassian-events app → POST /{jira|confluence}/webhook/{site}
receiver: verify Forge Invocation Token (RS256 vs Atlassian JWKS: sig, expiry, audience,
          pinned app id) + cloud-id ↔ site-row cross-check → dedup → bot-loop guard
          → ADF mention scan → resolve agent → build source_context → async router invoke
  ▼
router (unchanged order): identity.resolve("atlassian", accountId, …) → onboarding gate
  → trigger_authz (principal "atlassian:<accountId>", workspace=<site>, channel=<PROJECT|SPACE>,
    channelAllowed = container posture) → concurrency → guardrail → create_assignment
  → invoke_agent → threaded "🏁 on it" ack via reply.post_{jira|confluence}_comment
  ▼
agent runtime → gateway JiraTarget/ConfluenceTarget tools → result posted back in-thread
```

No mention + a matching enabled rule ⇒ the automation path (§A8). A non-ALLOW decision ⇒ `blocked_authz` assignment + `TriggerDenied` metric + a **specific** threaded reason (*"Reason: **project not onboarded**. Ask an admin, or check assignment `<id>`."*). First-touch users get the standard onboarding reply (org-copy variant — Atlassian reliably supplies a verified email).

## A4. The fine-grained access-control matrix

Six independent, composing controls; no single misconfiguration opens the whole surface. "Container" = Jira project or Confluence space.

| # | Question | Mechanism | Layer | Granularity |
|---|----------|-----------|-------|-------------|
| 1 | Who may trigger which agent from Atlassian? | `trigger_rule` rows (WHO) → AVP fixed policies | Dispatch Router (AVP) | user / permission-group × agent × site |
| 2 | Which containers may triggers come from? | `jira_proj#`/`confluence_space#` allow/deny rows + site posture → `context.channelAllowed` (P3 forbid) | Dispatch Router (AVP) | project / space |
| 3 | Which agent may call which tool? | `AGENT_TOOL_GRANTS` (built-ins) / capability `tool_grants` (custom) → `sdlc_permit_<agent>` Cedar permits | Gateway policy engine | agent × individual tool |
| 4 | Which containers may agents touch? | broker container allowlists (Confluence: reads included, §C2.4) + `sdlc_allowed_projects`/`sdlc_allowed_spaces` Cedar write forbids | Broker + Gateway Cedar | project / space |
| 5 | May this dispatch touch this container? (origin pinning) | interceptor Atlassian clauses: origin container / linked-repo co-scope | Gateway interceptor | dispatch origin × container |
| 6 | How do Confluence writes land? | per-space `write_mode: direct \| propose` + optional `write_agents` | Broker | space × agent × write style |

Layers 1–2 are data (DynamoDB writes, never a Cedar deploy). Layer 3 is the existing per-agent grant surface. Layers 4–6 exist because an Atlassian API token is **site-wide** — the credential cannot express container scope, so the brokers are the enforcement point (the same rationale as `scm_broker`'s co-repo enforcement).

## A5. Event transport — the `atlassian-events` Forge forwarder

Confluence Cloud has no admin-registered webhook UI, and Jira's System WebHooks require copy-pasted URLs + secrets that can silently rot. **One Forge app serves both products** (decided 2026-07-24, superseding the Jira draft's admin-registered webhook):

- **The app** (`forge/atlassian-events/` in this repo — one manifest, one thin forwarder module per product): Forge **product triggers** subscribe to the Jira set (comment created, issue created, issue updated — `avi:jira:*`) and the Confluence set (`avi:confluence:created:comment`, `created:page`, `updated:page`, label added/removed); each invocation forwards the event payload to the per-product fleet endpoint — `POST /jira/webhook/{site}` or `POST /confluence/webhook/{site}` (site id baked into each installation's environment at deploy time **and** cross-checked against the payload's `cloudId` — belt-and-braces). Scopes: read-only event scopes only — the app is *event transport only*; all REST reads/writes use the service-account token (§A6), never app auth. The manifest declares the fleet endpoint as the app's sole permitted external egress, so admins see exactly what data flows where before installing.
- **Delivery verification — no shared secret exists.** Every forwarded call carries a **Forge Invocation Token (FIT)**: an asymmetrically-signed JWT verified against **Atlassian's published JWKS** (RS256; keys fetched + cached with TTL, kid-rotation tolerated). Receivers verify signature, expiry, audience, and the pinned `forge_app_id`, then cross-check the installation cloud id against the `{site}` row. Bad/missing/expired ⇒ 401; JWKS unreachable ⇒ 503 (fail closed; Forge retries). A shared `verify_forge_invocation_token(...)` helper joins `verify_hmac_sha256`/`verify_slack_signature` in `mentions.py`. Consequence: no per-site webhook secret to capture, store, or rotate — no receiver-side `ssm:PutParameter` at all (T-9 surface deleted rather than scoped), and triggers are declarative manifest state that never expires (the strongest answer to the silent-deafness hazard that killed OAuth dynamic webhooks).
- **Distribution & install**: deployed once by fleet operators (`scripts/deploy_forge_atlassian.py` wraps `forge deploy` + environment wiring) under the fleet's Atlassian developer account; admins install per site via a **private installation link** (no development-mode toggle — unlike private Connect apps; this matters for enterprise orgs that prohibit dev mode). One install covers both products; events for a product with no onboarded/enabled site row are dropped at the receiver (inert-until-onboarded, per product).
- **Accepted coupling & costs**: the app is one CLI-deployed artifact outside the SAM stack (amortized across both products); a manifest change prompts admin upgrade-consent on every installed site; uninstalling silences **both** products on that site — surfaced by the site's per-product `webhook_last_seen` liveness going stale on the connector page (§A10). Subject to Forge runtime quotas (comfortably above webhook-forwarding volumes).
- **Rejected alternatives**: Atlassian Connect (dev-mode requirement for private installs, per-site sharedSecret lifecycle to manage, deprecation track); Jira admin-registered System WebHooks (manual copy-paste registration + stored HMAC secret per site — superseded); OAuth-app dynamic webhooks (30-day expiry ⇒ silent deafness; rejected in the original draft, stays rejected).
- **Pre-build verification (Phase-3 gate, §A13):** confirm current Forge trigger coverage delivers (a) Jira comment/issue events with changelog payloads sufficient for §B1, and (b) Confluence **inline-comment** creation events with enough payload to resolve the comment. If a gap surfaces, the affected event falls back per product (Jira: static System WebHook for that event; Confluence: Connect) — the receiver contract is transport-agnostic by construction, so a fallback is additive, not a rewrite.

## A6. Site & credential model

### A6.1 One site record — `pk="atlassian_site#<site_id>"`

One row per Atlassian site (cloud id), covering both products:

```jsonc
{
  kind: "atlassian_site",
  site_id: "<cloud_id>",              // Atlassian cloud id (uuid); validated ^[0-9a-f-]{36}$
  site_url: "https://acme.atlassian.net",
  site_name: "Acme",
  enabled: true,
  products: { jira: true, confluence: true },   // per-product enablement (a receiver drops events for a disabled product)
  bot_account_id: "712020:abc…",      // ONE fleet service account — mention anchor + bot-loop guard for both products
  bot_email: "sdlc-agents@acme.com",
  api_token_param: "/sdlc-agents/<stage>/atlassian/<site_id>/api-token",  // SSM SecureString — ONE token, both products
  forge_app_id: "ari:cloud:ecosystem::app/…",   // pinned at first verified delivery (§A5) — no webhook secret exists
  webhook_last_seen: { jira: <epoch|null>, confluence: <epoch|null> },   // liveness, stamped by the receivers
  token_expires_at: <epoch|null>,     // API tokens expire (max 1 yr) → credential_expired notify (14/3/0 days out)
  default_project_policy: "allowlist" | "denylist",   // Jira WHERE posture
  default_space_policy:   "allowlist" | "denylist",   // Confluence WHERE posture (allowlist strongly recommended — §C2.4)
  onboarded_by, onboarded_at,
  status: "pending" | "active" | "disabled"
}
```

The API token is per site, SecureString, fetched per-invocation (T-8/T-36). Multi-site = multiple rows. Container rows stay per product (§B1.2, §C1.2) — projects and spaces have different fields and postures.

### A6.2 Service account + API token (recommended); OAuth 3LO (alternative)

A dedicated Atlassian service account (`sdlc-agents@acme.com`, display "SDLC Agents") with a **scoped API token** (Basic auth to REST). Rationale (unchanged from the pre-merge specs): it is the mention anchor (`@SDLC Agents` is real in both products' mention pickers), the exact bot-loop filter (`author.accountId == bot_account_id`), and the attribution identity for every comment, ticket change, and page version — each write carries the acting agent's signature line (`🤖 **[Docwriter Agent]** · run <assignment_id>`). It also matches the fleet's easiest onboarding UX: paste-a-token, like Slack's one-step connect — no OAuth callback surface, no rotating-refresh-token serialization hazard. Scoped tokens SHOULD be used (classic unscoped accepted but flagged); expiry is tracked, alerted, and shown as a countdown badge. OAuth 3LO via `scripts/bootstrap_jira_oauth.py` remains the documented alternative for orgs that forbid service accounts (needs a token-refresh lock row; documented, not scheduled).

### A6.3 Identity & principals

- **Human principal:** `atlassian:<accountId>` — immutable, global across sites and products (T-4; never the display name). Applied centrally in `router.namespaced_principal` for both `source=="jira"` and `source=="confluence"`. One handle key (`identity.handles.atlassian`), so a person onboarded via either product is the same identity in the other automatically.
- **Automation principal:** `automation:<connector>:<rule_id>` (`automation:jira:…` / `automation:confluence:…`) — synthetic, per-rule; distinct namespace so a rule can never inherit a person's grants (§A8.4).
- **Enrichment:** webhook/REST author objects supply `accountId`, `displayName`, and (when site privacy settings allow) `emailAddress` from Atlassian's authenticated directory ⇒ seeds the identity map **verified** (`email_verified=True`), joining the person to their GitHub/Slack/Asana handles (T-42 discipline). Email hidden ⇒ email-less identity; standard merge paths.

## A7. Trigger authorization — zero new Cedar

The AVP `TriggerPolicyStore` fixed 3-policy set is untouched:

- **WHO** — `trigger_rule` rows with `connector:"jira"` or `"confluence"` (per-product rules, so "may trigger from Jira" ≠ "may trigger from Confluence"); subjects are `atlassian:<accountId>` principals, **permission groups** (a group grant authored once already applies — the identity-map payoff), or automation principals.
- **WHERE** — `context.channelAllowed` computed by `trigger_grants.container_allowed(site_id, container_key, product)` (one generalized sibling of `channel_allowed`, posture math over the product's container rows + the site's per-product default policy). `context.workspace` = site id; `context.channel` = project key or space key. Unknown/disabled site or product ⇒ False (fail-closed).
- **Fail-closed invariants** unchanged: unresolved sender rejected pre-AVP; grant-read failure ⇒ `authz-unavailable`; deny ⇒ `blocked_authz` row + `TriggerDenied` metric + threaded reason.

## A8. Event automation rules (new engine, shared)

A data-driven **event → agent** engine: `automation_rule#` rows matched by receivers against normalized event facts, dispatching through the **unmodified** router spine. Source-agnostic by schema (`connector` field) — Atlassian ships it; GitHub/Asana rules are a fast-follow with zero schema work.

### A8.1 Rule record — `pk="automation_rule#<uuid>"`

```jsonc
{
  kind: "automation_rule",
  rule_id: "<uuid>",
  connector: "jira" | "confluence",
  enabled: true,
  event: /* jira: */ "issue_transitioned" | "issue_created" | "issue_commented" | "issue_assigned"
         /* confluence: */ | "page_labeled" | "page_created" | "page_updated",
  match: {                            // ALL present keys must match (AND); values exact or "*"
    site: "<site_id>" | "*",
    project: "ENG" | "*",             // jira
    space: "DOCS" | "*",              // confluence
    to_status: "Code Review", from_status: "*", issue_type: "*",   // issue_transitioned
    label: "sdlc-review",             // page_labeled
    title_contains: "…",              // confluence, optional
    labels_any: ["needs-review"]      // optional OR-set
  },
  action: { agent_id: "adr",
            instruction_template: "Review the code for {{issue_key}}: {{summary}}. PRs are linked on the issue." },
  cooldown_seconds: 3600,             // per (rule, unit) dedup window — loop/flap brake
  created_by, created_at, updated_at, last_fired_at, fire_count
}
```

Template variables are allowlisted per event family (jira: `{{issue_key}} {{summary}} {{project}} {{status}} {{from_status}} {{to_status}} {{issue_type}} {{reporter}} {{assignee}} {{site_url}}`; confluence: `{{title}} {{space}} {{page_id}} {{page_url}} {{label}} {{author}}`); unknown variables render empty. Rendered instructions pass the edge guardrail like any user text (summaries/titles are user text — T-1 applies).

### A8.2 Matching — `infra/dispatch/automation.py`

`automation.match(connector, event, facts) -> list[Rule]` — enabled rules via the standard `kind-index` + 30s TTL cache; AND over present keys; `"*"` wildcards; `labels_any` OR-set. Called by a receiver **only when no mention resolved** (a mention is explicit intent and wins). Per-rule cooldown dedup: TTL'd `auto-fire#<rule_id>#<unit>#<qualifier>` items (assignments-table shape) suppress re-fires inside `cooldown_seconds` (unit = issue key or page id).

### A8.3 Dispatch semantics

Rendered instruction → normal dispatch payload with `trigger_type:"automation"`, `sender:"automation:<connector>:<rule_id>"`, `agent_id` pre-resolved. Everything downstream is stock: guardrail, concurrency caps, assignment records `trace_refs.automation_rule_id` (dashboard answers "what did this rule run?").

### A8.4 Authorization — rules are grants, not bypasses

Rule create/enable is admin-gated; the admin API auto-authors a **permit `trigger_rule`** for the rule's synthetic principal (deterministic rule id — the channel-approval overwrite pattern) and removes it on delete/disable. Automation dispatches therefore evaluate through the same AVP path — deleting the grant kills the rule's power even if a stale cache still matches it (default-deny backstop). The WHERE axis also applies: a rule on a non-onboarded container is dead on arrival (P3 forbid).

### A8.5 Loop safety

Four independent brakes: (1) actor guard — events by `bot_account_id` never match rules, so an agent's own comment/transition/page-write can never re-trigger a rule on the same unit; (2) per-(rule, unit) cooldown; (3) a per-rule hourly fire ceiling (`AUTOMATION_MAX_FIRES_PER_HOUR`, default 20) with `AutomationRuleThrottled` metric + error-tier notification; (4) chain-depth cap — automation-triggered runs carry the chain of rule ids in dispatch context; a rule may not match an event whose chain already contains it; depth cap 3. (Cross-agent pipelines — an agent's transition firing a *different* agent's rule — remain a feature within these bounds.)

## A9. Notifications

### A9.1 Channel subscriptions (existing system, extended)

New events in `slack_notify.TIER_EVENTS` — **actionable:** `agent_replied` (an agent replied to/mentioned a person), `doc_proposal_ready` (a propose-mode Confluence proposal awaits a human); **informative:** `agent_commented`, `issue_transitioned`, `page_published`, `automation_fired`; **error:** `automation_throttled`, `credential_expired` (now fired by the Atlassian token-expiry check). `notif_sub` rows gain optional `projects` and `spaces` scopes alongside `repos`, validated ⊆ onboarded containers (the T-43 discipline); `notify.notify()` gains the matching axes. Threading: `unit` = issue key or page id, so a unit's lifecycle collapses into one thread per channel. The `/sdlc-notify` modal gains project/space multi-selects (same 75-char index-value trick as repos). Emitter split stays disjoint (no double-notifies): receivers emit Atlassian-side events, the automation engine emits its own, and the assignments-table stream Lambda owns terminal run events.

### A9.2 Per-user Slack DMs (new surface) — `pk="notif_pref#<identity_id>"`

Opt-in, self-serve: `/sdlc-notify me` opens a personal modal → tier/event checkboxes → writes the pref row (only an **active** identity with a **verified** Slack handle may hold prefs). Delivery: `notify.notify_user(identity_id, tier, event, text, unit)` — `conversations.open` (`im:write` scope, manifest bump) + threaded `chat.postMessage`; no verified handle for the chosen team ⇒ **silent degrade** (metric, never a mis-ping). Targeting: `agent_commented` → issue reporter + assignee / page watchers-of-record (reporter+assignee analog: page author); `agent_replied` → users the agent's comment @mentions (ADF mention nodes) — each accountId → identity → pref check → DM. The same seam serves non-Atlassian events free: `run_completed`/`run_failed`/`awaiting_approval` DMs to the requester from `assignment_notifier.py`, one new call site. Anti-spam: `min_tier` honored; channel-mention/DM dedup key `(identity, event, unit)` short-TTL; informative DMs digest-batched per unit thread.

## A10. Connectors UI — `dashboard/src/connectors/AtlassianConnectorPage.tsx`

**One connector page** for the suite (registry `id: "atlassian"`; health badge = site count + per-product liveness + token-expiry warnings). Route `#/admin/connectors/atlassian`. Tabs (Slack page = chrome template):

- **Sites** — the guided connect (§A12): service-account checklist → paste site URL + service-account email + API token → **Connect** (one POST verifies the token, resolves `bot_account_id` + cloud id, stores the SecureString, writes the row) → per-product enable toggles → **app-install card** (the Forge app's private installation link + manifest scopes/egress; "installed" state shown per site) → **Verify delivery** per product (reads `webhook_last_seen`). Per-site enable/disable/remove, token-expiry countdown.
- **Jira projects** — allow/deny rows + posture toggle + per-project repo scope (project list fetched live).
- **Confluence spaces** — allow/deny rows + posture toggle + per-space **write mode** (`propose`/`direct`), **write agents**, linked-repo scope (space list fetched live; the onboarding modal states plainly: *"onboarding a space makes it readable by all agents"* — §C2.4).
- **Access rules** — `TriggerRulesPanel` with a product filter (`connector="jira"` / `"confluence"`) + the **Test access** simulator (subject × agent × site × container → ALLOW/DENY + deciding policy).
- **Automations** — §A8 rule builder: event/site/container/status-or-label pickers (fetched live), agent dropdown (registry), template editor with variable chips + preview, enable toggle, per-rule activity.
- **Notifications** — channel subs with project/space scopes (admin view); link to per-user prefs on Access → Users.
- **Activity** — `ActivityPanel` with a source filter (`jira`/`confluence`) + receiver errors + `TriggerDenied` + `AutomationRuleThrottled`.

`types.ts`: `AtlassianSite`, `JiraProject`, `ConfluenceSpace`, `AutomationRule`, `NotifPref`; `TriggerRule.connector` union += `"jira" | "confluence"`. `api.ts`: `listAtlassianSites/connectAtlassianSite/deleteAtlassianSite/setAtlassianProducts`, `listJiraProjects/putJiraProject/deleteJiraProject`, `listConfluenceSpaces/putConfluenceSpace/deleteConfluenceSpace`, `listAutomationRules/createAutomationRule/updateAutomationRule/deleteAutomationRule`, `getNotifPref/putNotifPref`, `verifyAtlassianWebhook(siteId, product)`.

## A11. Admin API routes (`infra/dashboard/admin.py`)

All `auth.is_admin`, fail-closed, `_route` pattern:

| Method + path | Purpose |
|---|---|
| `GET/POST /admin/atlassian/sites`, `POST …/connect`, `PUT …/{site_id}/products`, `DELETE …/{site_id}` | Site CRUD; `connect` = verify token + resolve cloud id/bot account + store SecureString + write row (the Slack one-step-connect pattern) |
| `POST /admin/atlassian/sites/{site_id}/verify-webhook?product=` | Per-product delivery liveness (reads receiver-stamped `webhook_last_seen`) |
| `GET/POST /admin/atlassian/projects`, `DELETE …/{site_id}/{key}` | Jira project allow/deny + repo scope. **Writes run `_sync_after_write`** (rows render into `sdlc_allowed_projects`) |
| `GET/POST /admin/atlassian/spaces`, `DELETE …/{site_id}/{key}` | Confluence space allow/deny + write mode/agents + repo scope. **Writes run `_sync_after_write`** (rows render into `sdlc_allowed_spaces`) |
| `GET/POST /admin/automation-rules?connector=`, `PUT/DELETE …/{rule_id}`, `POST …/{rule_id}/enable\|disable` | Rule CRUD; create/enable auto-authors the automation grant, delete/disable removes it (§A8.4) |
| `GET/PUT/DELETE /admin/notif-prefs/{identity_id}` | Per-user DM prefs (admin + self) |
| *(existing)* `/admin/trigger-rules?connector=jira\|confluence`, `/admin/trigger-rules/simulate` | WHO rules + simulator — connector param only |

IAM: the connect route needs `ssm:PutParameter` scoped to `/sdlc-agents/${Stage}/atlassian/*` (the API token — the only secret; the Forge transport has none) — the one deliberate deviation from the Slack posture, justified because paste-token *is* the first-class onboarding. No AVP permissions (grants are data; simulator stays local).

`config_store` additions, mirrored style, each paged + id-validated (validators: cloud id, project key `^[A-Z][A-Z0-9]{1,9}$`, space key `^[A-Z][A-Z0-9]{0,254}$` (rejects `~`), account id `^[0-9a-z:-]{1,128}$`, page/comment ids numeric, label `^[a-z0-9][a-z0-9-]{0,63}$`): `list/get/put/delete_atlassian_site`, `set_atlassian_site_status`, `set_atlassian_products`; `list/put/delete_jira_project(site_id, …)`; `list/put/delete_confluence_space(site_id, …)`; `list/get/put/delete_automation_rule`, `set_automation_rule_enabled`; `get/put/delete_notif_pref`.

## A12. First-class onboarding UX

**Skill:** `skills/sdlc-agents-connect-atlassian/SKILL.md` (template: `sdlc-agents-connect-asana` — trigger phrasing, auth-channel explanation, SSM paths, inline verification probes, pitfalls table, `selection.yaml` recording, explicit non-goals). `skills/sdlc-agents/SKILL.md` Step-1 discovery flips Jira from "planned" to a real connect path; `sdlc-agents-select` `pm: jira` becomes valid. `docs/aws-deploy.md` gains the SSM rows + the Forge deploy step.

**Admin walk-through (~10 minutes, both products):**

1. In Atlassian: create the `sdlc-agents` service account, grant it the target projects' work permissions + spaces' page permissions, mint a scoped API token (1-yr max).
2. Dashboard → Connectors → Atlassian → **Connect a site**: paste site URL + service-account email + token → Connect → toggle on Jira and/or Confluence.
3. **App-install card**: install the `atlassian-events` Forge app from its private link (scopes + egress shown up front; no secret changes hands) → **Verify delivery** per product → green checks.
4. **Jira projects**: onboard `ENG` (allowlist), attach its repo scope. **Confluence spaces**: onboard `DOCS` (allowlist, `write_mode: propose`), link repos.
5. **Access**: grant a permission group the WHO rules per product; simulator to confirm (existing group grants already apply — identity-map payoff).
6. Users `@sdlc-agents workitems …` on an issue, or `@SDLC Agents docwriter …` in a page comment. First-touch users get the onboarding reply → admin approves in Access → Users.
7. Later: flip `DOCS` to `direct` when trust is established; author the first automation rules from templates; users run `/sdlc-notify me` for DMs.

## A13. Delivery phases (the unified plan)

Each phase is independently shippable and test-gated; §A2's enum checklist lands with the phase that first needs each entry. Ordering interleaves the two products by value: context reads first (no transport needed), then ticket + doc tools, then dispatch on both surfaces, then UI, then automation/notifications.

1. **Shared foundation + Confluence read path (deep context).** `atlassian_site#` model + connect/products admin routes + enums/validators, identity `atlassian` handle + `namespaced_principal`, `confluence_broker.py` (read tools) + `ConfluenceGatewayTarget` + interceptor clause, `fleet_policy` read grants + `CONFLUENCE_TOOL_CLASS`, `cedar/confluence.cedar`. *Exit: a site connects; every granted agent greps team docs via CQL and reads pages from onboarded spaces; non-onboarded spaces invisible; `check_gateway_manifest.py` passes.*
2. **Jira agent tools + Confluence write path.** `jira_broker.py` + `JiraGatewayTarget` + interceptor clause + `JIRA_TOOL_CLASS` + grants + `sdlc_allowed_projects` + `cedar/jira.cedar`; Confluence write tools + Markdown↔storage converter + `base_version` guard + `sdlc_allowed_spaces` + `write_mode`/`write_agents` enforcement; `jira_dispatch_block()`/`confluence_dispatch_block()`; prompt updates; delete `agents/workitems/tools/jira_mcp.py`. LOG_ONLY → ENFORCE rollout. *Exit: workitems reads/writes tickets, adr/docwriter comment-only on Jira; docwriter publishes to a `direct` space and proposes in a `propose` space; researcher read-only everywhere.*
3. **Dispatch on both surfaces.** *(Gate: the §A5 pre-build verification.)* `forge/atlassian-events/` app (both modules) + `deploy_forge_atlassian.py` + `verify_forge_invocation_token`, `jira_webhook.py` + `confluence_webhook.py` (FIT verify/dedup/bot-loop/ADF walker — shared `_adf_to_text` — mention scan/inline selection/context front-load), `reply.post_jira_comment`/`post_confluence_comment`, router + identity + enrichment + notifier branches, template resources + alarms. *Exit: mentions on issues and page/inline comments dispatch; replies land in-thread; rejects/onboarding replies work; existing suites green.*
4. **Onboarding UI.** `AtlassianConnectorPage` (all tabs except Automations), registry/types/api/App/format wiring, admin SSM write grant. *Exit: the §A12 walk-through works end-to-end with no CLI.*
5. **Automation + notifications.** `automation.py` engine + receiver hooks + rule CRUD/grant coupling + Automations tab + throttle alarm; `TIER_EVENTS` additions, `projects`/`spaces` sub scopes, `notif_pref#` + `notify_user` DM path (`im:write` manifest bump), `/sdlc-notify me`, `assignment_notifier` requester-DM call, token-expiry check. *Exit: "Code Review → adr" and "sdlc-review label → adr" rules fire, are audited, throttleable, and die when disabled; a user opts in and gets threaded DMs; no mis-pings (degrade tested).*
6. **Docs, skills, threat model, rollout.** Connect skill + quickstart flip, `aws-deploy.md`, threat-model rows (§A15), CLAUDE.md table row, deploy dev → gamma → prod with `DeployJiraTarget`/`DeployConfluenceTarget=true` after grant merge.

## A14. Testing (unified)

Mirrors the per-module style of the Slack suites; every phase lands with its tests.

- **Receivers (`jira_webhook`, `confluence_webhook`)**: valid FIT → correct dispatch payload; bad signature/expired/wrong audience/wrong app id → 401; JWKS unreachable → 503; kid-rotation → refetch+verify; unknown/disabled site or product → drop; cloud-id / `issue.self` host mismatch → drop+metric; dedup (same delivery twice → one dispatch; fail-open on store error); bot-actor events never dispatch/match rules but do feed notify; ADF mention resolution + text fallback + unknown-mention no-op; comment history/thread front-loaded; Confluence inline selection captured; context-fetch failure still dispatches base context; automation matched only when no mention.
- **Brokers (`jira_broker`, `confluence_broker`)**: tool dispatch by name; container allowlists (Jira writes; Confluence **reads and writes**); arg-consistency (issue-key prefix ↔ `project_key`; page ↔ `space_key`); CQL space pin unwidenable; `list_spaces` filtered; `base_version` stale-write rejection; `write_mode: propose` rejects page writes with instructive error but allows `add_comment`; `write_agents` narrowing; per-agent tier fail-closed on missing `_dispatch_agent`; Markdown→storage escaping (no XHTML/macro injection); ADF wrapping of Jira comment bodies; tokens fetched per-invocation; `tool_definitions` ↔ template pin-tests; destructive-tool absence pinned.
- **`fleet_policy`/`policy_sync`**: both targets' grants render; `sdlc_allowed_projects`/`sdlc_allowed_spaces` render from rows (`unless {false}` when empty); `classify_tool` on both name sets; undeployed-target filtering.
- **`trigger_grants`**: `container_allowed` posture math per product (allowlist/denylist/unknown-site-closed/other-source-open); automation-principal grant resolution.
- **Interceptor**: Atlassian clauses origin pinning (own container allowed; unlinked container rejected via tool-error shape, never a JSON-RPC error; body type preserved dict-in/dict-out).
- **`automation`**: match semantics (AND keys, wildcards, labels_any, per-connector fact sets); cooldown suppress; hourly ceiling; chain-depth guard; template allowlists (unknown var → empty); disabled rule inert; grant auto-author/remove on create/delete.
- **`notify`/`slack_notify`**: project/space scope matching; `notify_user` (opt-in only, verified-handle only, silent degrade, DM/channel dedup, threading per unit); new events in tiers; `/sdlc-notify me` round-trip.
- **`config_store`/`admin`**: new-kind CRUD + id validation (Cedar-metachar, `~` space keys, bad project keys); connect route (token-verify mock, SecureString write, per-product toggles, idempotent re-connect); container writes run `_sync_after_write` with rollback-on-enforcing semantics; automation routes incl. grant coupling; notif-pref self-vs-admin authz; `is_admin` gating throughout.
- **`enrichment`/`reply`/router**: native jira + confluence trace_refs/participants; `post_jira_comment`/`post_confluence_comment` bool contracts (+ Confluence parent-comment threading); authz-deny posts threaded reason + `blocked_authz` + `TriggerDenied`.
- **SPA**: `#/admin/connectors/atlassian` routing; registry descriptor unique/routable; `sourceLink` branches; `npm run build` green.

## A15. Threat-model additions (`docs/threat-model.md`)

New components: **C-23 Atlassian service account + `atlassian-events` Forge forwarder**, **C-24 Jira receiver + broker/target**, **C-25 Confluence receiver + broker/target**, **C-26 automation-rule engine**. New threats (one contiguous block, replacing the two pre-merge lists):

| ID | Threat | Mitigation |
|----|--------|-----------|
| T-46 | Webhook forgery / cross-site event confusion | per-delivery Forge Invocation Token verified RS256 against Atlassian's JWKS (signature, expiry, audience, pinned app id); cloud-id + host cross-checks; fail-closed when JWKS unavailable; no shared secret exists to steal or replant (§A5) |
| T-47 | Silent webhook deafness | Forge triggers are declarative manifest state (never expire); per-product `webhook_last_seen` liveness on the connector page; app uninstall silences both products — surfaced on the same card (§A5, §A10) |
| T-48 | Site-wide token blast radius (no per-container credential exists) | broker container allowlists (Confluence: reads included) + Cedar container write forbids + interceptor origin pinning + curated no-delete schemas (§A4, §B2, §C2) |
| T-49 | Automation loops / event storms | bot-actor guard, per-(rule,unit) cooldown, hourly ceiling + throttle alarm, chain-depth cap (§A8.5) |
| T-50 | Automation privilege escalation (a rule as a grant side door) | rules admin-gated; synthetic per-rule principal authorizes through the same AVP path via an auto-managed grant; default-deny backstop (§A8.4) |
| T-51 | Service-account token theft / expiry | SecureString per site, per-invocation fetch (T-8), scoped tokens recommended, expiry tracked + `credential_expired` alerts (§A6.2) |
| T-52 | DM mis-ping / notification identity confusion | prefs keyed on identity_id; active + verified-handle required; silent degrade on unresolved; DM/channel dedup (§A9.2) |
| T-53 | Instruction injection via automation templates | allowlisted variables only; rendered instruction passes the edge guardrail like any user text (§A8.1) |
| T-54 | Storage-format / macro injection via agent-authored Confluence bodies | Markdown-only tool boundary; broker-owned conversion, macro-free subset, XML-escaped output (§C2.2) |
| T-55 | Prompt injection via issue/page/comment content read as context | tickets and pages are untrusted user text (T-1 family): the fail-closed model-attached guardrail covers every model call; writes remain gateway-Cedar-scoped regardless of what the model was told |
| T-56 | Silent doc corruption at scale (a misbehaving agent rewriting many pages) | default `write_mode: propose`; version history + attribution for audit/rollback; `base_version` guard; per-space `write_agents`; concurrency caps; `page_published` notifications make direct-mode writes visible (§C3) |
| T-57 | Stale-write clobbering of concurrent human edits | `base_version` optimistic concurrency, broker-rejected with re-read guidance (§C2.2) |
| T-58 | Confidential-space exposure via over-broad onboarding | "onboarding a space = fleet-wide read" stated in the UI at the decision point; allowlist posture recommended; reads logged with space dimension (§C2.4, §A10) |

Plus DF rows for: the Forge inbound flows (forwarder → receivers → router, FIT-authenticated), both brokers' outbound flows, automation dispatch, the propose/apply comment loop, and the DM path; a changelog row; risk summary recounted.

## A16. Open decisions

1. **Assignment-style Jira trigger** (assign the issue to the service account + an "Agent" select field, mirroring Asana) — deferred; mentions + automation cover the use cases. Revisit on demand.
2. **OAuth 3LO build-out** (§A6.2 alternative) — spec'd, not scheduled. Trigger: a customer org that prohibits service accounts.
3. **Agile tools** (boards/sprints, read-only) and **Confluence attachments** (`add_attachment` with size/type allowlist) — additive to the curated schemas when an agent needs them.
4. **Automation for GitHub/Asana events** — the engine is source-agnostic; wiring their receivers is a fast-follow after Phase 5 proves the model.
5. **Sub-space scoping** (restrict agents to a page subtree via a root `parent_id`) — the broker could enforce ancestry; deferred (space-per-concern is the recommended modeling).
6. **Per-agent read narrowing** (`read_agents` mirroring `write_agents`) — deferred; container-level onboarding granularity has been sufficient for the analogous repo case.
7. **AgentCore Memory for cross-run context** — unchanged from the fleet roadmap; comment-history front-loading is sufficient for v1 conversations.
8. **Confluence page-watch DMs** ("DM me when an agent edits a page I watch") — rides `notif_pref#` + the watchers REST read; not scheduled.

---

# Part B — Jira capability

## B1. Dispatch source

### B1.1 Receiver — `infra/dispatch/jira_webhook.py`

`jira-webhook-<Stage>` Lambda, route `POST /jira/webhook/{site}`, fed by the Forge forwarder (§A5). Always deployed, inert until a site row enables `jira`. Correctness requirements beyond the shared §A5 verification:

- **Events**: comment created, issue created, issue updated (changelog-bearing). Mention scan on `comment_created`; automation matching on the rest (and on unmentioned comments).
- **Mention detection — ADF first, text fallback.** Scan the comment ADF for a `{"type":"mention","attrs":{"id":<bot_account_id>}}` node; agent id = first registry-resolvable token after the mention in the flattened text (shared `_adf_to_text` walker — do not regex the JSON). Fallback: `mentions.resolve_mention` on flattened text (`@workitems` form). Unknown mention ⇒ 200 no-op.
- **Context front-loading (GitHub-style).** `source_context` = issue snapshot (`site`, `project_key`, `issue_key`, summary, status, type, `comment_id`) + **all comments** flattened as `"[<displayName> at <iso>]:\n<text>"` blocks (one REST call + `…/comment`), best-effort — fetch failure still dispatches base context. Every follow-up mention re-dispatches with the full thread ⇒ multi-turn conversation on an issue.
- **Dedup** on TTL'd `jira-event#<sha256(eventType + issue.id + comment.id|changelog.id + timestamp)>` items (the `slack_event_dedup` shape) — check-before / mark-after-success; fail-open on store errors.
- **Bot-loop**: events by `bot_account_id` never dispatch or match rules — but agent-authored `comment_created` feeds the notification path (§A9) only.
- **Ack discipline**: verify → dedup → async `Event`-invoke → 200; the "🏁 on it" ack is posted by the router.

### B1.2 Project policy — `pk="jira_proj#<site_id>#<KEY>"`

```jsonc
{ kind: "jira_project", site_id, project_key: "ENG", project_name,
  mode: "allow" | "deny",
  repos: ["owner/repo", …],   // the project's linked-repo co-scope edge
  note, created_by, created_at }
```

Interpreted against the site's `default_project_policy` (§A6.1); drives the WHERE axis (§A7) and the `sdlc_allowed_projects` Cedar forbid (§B2.3).

### B1.3 Replies

`reply.post_jira_comment(site_id, issue_key, body) -> bool` — site token per-invocation; `POST /rest/api/3/issue/{key}/comment`, body wrapped in minimal ADF (`_text_to_adf`); non-fatal + metric on failure. Used by the router for acks, guardrail blocks, authz rejects, onboarding replies. Agents post their final answers via `JiraTarget___add_comment` (gateway-only chokepoint), steered by `jira_dispatch_block()`'s `Reply to:` line (site/project/issue/status/comment history + the project's repo scope).

## B2. Gateway target: `JiraTarget` + broker + Cedar

### B2.1 Broker — `infra/dispatch/jira_broker.py`

Lambda target (GATEWAY_IAM_ROLE credential type), curated `InlinePayload` schema — the remote Atlassian MCP is rejected (Atlassian-defined tool surface incl. deletes, DYNAMIC-listing trap, no container scoping; §A4 layer-4 rationale). Dispatches on `bedrockAgentCoreToolName`; reads interceptor-injected `_dispatch_agent`/`_dispatch_origin`; derives `project_key` from the issue-key prefix and **requires arg-consistency** (explicit `project_key` must match); checks the project allowlist + the agent's grant class; then REST v3 with the site token. Fails closed on missing agent tier or unknown site/project.

### B2.2 Curated tool set (v1)

| Tool | Class | Args (required) |
|---|---|---|
| `get_issue` / `get_issue_comments` / `get_transitions` | read | site, issue_key |
| `search_issues` | read | site, jql, max_results≤50 |
| `list_projects` | read | site |
| `get_project` | read | site, project_key |
| `add_comment` | write | site, issue_key, project_key, body |
| `create_issue` | write | site, project_key, issue_type, summary (+description, labels) |
| `update_issue` | write | site, issue_key, project_key, fields{summary/description/labels/priority} |
| `transition_issue` | write | site, issue_key, project_key, transition_name (+resolution) |
| `assign_issue` | write | site, issue_key, project_key, account_id |
| `link_issues` | write | site, inward_key, outward_key, project_key, link_type |
| `add_remote_link` | write | site, issue_key, project_key, url, title |

Absent by construction: any delete, worklog writes, sprint/board writes, project/user admin. `tool_definitions()` generates the schema; pin-test keeps the template copy in sync; `check_gateway_manifest.py` coverage extends to `JiraTarget` before ENFORCE.

### B2.3 Cedar grants

`fleet_policy.py`: `JIRA_TARGET = "JiraTarget"`, `JIRA_TOOL_CLASS` (table above), `classify_tool()`/`tool_catalog()` extension, and `AGENT_TOOL_GRANTS`:

| Agent | Jira grants |
|---|---|
| `workitems` | all reads + `add_comment, create_issue, update_issue, transition_issue, assign_issue, link_issues, add_remote_link` — the PM owns ticket state |
| `adr` / `docwriter` | all reads + `add_comment, add_remote_link` |
| `researcher` | reads only |

Fleet forbid `sdlc_allowed_projects`: Jira write tools forbidden `unless { context.input.project_key == "ENG" || … }`, rendered from onboarded-project rows exactly like `sdlc_allowed_repos` (empty ⇒ `unless { false }`). Advisory mirror `cedar/jira.cedar` + `shared.cedar` delete-family forbids (documentation of intent; structurally absent anyway). Interceptor clause: a Jira-originated dispatch may act on its own project (+ its `repos` for GitHub tools); a GitHub-originated dispatch may act on projects whose `repos` include the origin (edge read in reverse). Prompt rules: "NEVER transition an issue to Done/Closed without explicit human approval on the issue"; signature line; honest-error-reporting.

## B3. Traceability

`derive_trace_refs`: `source=="jira"` emits `jira_key`/`jira_project`/`jira_site` natively (the regex hint keeps working for other sources — a GitHub PR mentioning ENG-142 still joins; `jira_key` is already in `TRACE_DIMENSIONS`). `derive_participants`: reporter/assignee/commenter with `source:"jira"`. Jira-side: agents stamp **remote links** for every artifact produced (PR URL, created issue, dashboard run deep-link); GitHub issues created from a Jira issue carry `[ENG-142]` + a `tracked-in-jira` label, closing the loop on later GitHub-side runs. `format.sourceLink` → `https://<site>/browse/<KEY>`.

---

# Part C — Confluence capability

## C1. Dispatch source

### C1.1 Receiver — `infra/dispatch/confluence_webhook.py`

`confluence-webhook-<Stage>` Lambda, route `POST /confluence/webhook/{site}`, fed by the Forge forwarder (§A5). Always deployed, inert until a site row enables `confluence`. Beyond the shared verification:

- **Events**: comment created (mention scan), page created/updated + label added/removed (automation matching).
- **Mention detection**: webhook payloads don't carry full bodies reliably — on `comment_created`, fetch the comment via REST (`/wiki/api/v2/comments/{id}?body-format=atlas_doc_format`), scan for the bot mention node, resolve the agent (shared walker + fallback, as §B1.1).
- **Inline comments carry the selection.** The REST fetch includes the anchored text (`inlineProperties.originalSelection`) → `source_context.inline_selection` — this is what makes "update *this section*" actionable.
- **Context front-loading**: page snapshot (`page_id`, `page_title`, `space_key`, page version) + the **full comment thread** flattened, best-effort. Follow-up mentions re-dispatch with the whole thread ⇒ propose → approve → apply works in a comment thread.
- **Dedup** on `confluence-event#<sha256(event + page_id + comment_id|label + timestamp)>`; bot-loop and ack discipline as §B1.1.

### C1.2 Space policy — `pk="confluence_space#<site_id>#<KEY>"`

The WHERE axis **and** the write-safety axis:

```jsonc
{ kind: "confluence_space", site_id, space_key: "DOCS", space_name,
  mode: "allow" | "deny",
  write_mode: "direct" | "propose",   // §C3.2 — default "propose"
  write_agents: ["docwriter"],        // optional narrowing; [] = any Cedar-granted agent
  repos: ["owner/repo", …],           // linked-repo co-scope edge
  note, created_by, created_at }
```

### C1.3 Replies

`reply.post_confluence_comment(site_id, page_id, body, parent_comment_id=None) -> bool` — site token per-invocation; `POST /wiki/api/v2/footer-comments` (or a reply under `parent_comment_id`, covering inline threads); body converted Markdown → storage format via the shared converter (§C2.2); non-fatal + metric. Agents answer via `ConfluenceTarget___add_comment`, steered by `confluence_dispatch_block()` (site/space/page/title/version, the thread, the **inline selection**, the space's `write_mode` — so the agent knows up front whether to apply or propose — and the space's repo scope).

## C2. Gateway target: `ConfluenceTarget` + broker + Cedar

### C2.1 Broker — `infra/dispatch/confluence_broker.py`

Same shape as §B2.1, plus a fourth broker duty Cedar can't perform: **write-mode enforcement** (§C3.2). Every tool requires `site` and `space_key`; page-level tools also take `page_id`, and the broker **cross-checks the page's actual space against the `space_key` arg** (an agent cannot reach page X in space B by claiming space A). Check order: known tool → onboarded+allowed space (**reads included**, §C2.4) → agent grant-class sanity → write-mode/write-agents for writes → REST with the site token → structured log `{tool, site, space, page, agent, origin, outcome, latency_ms}`.

### C2.2 Curated tool set (v1)

| Tool | Class | Args (required) | Notes |
|---|---|---|---|
| `get_page` | read | site, space_key, page_id | body as Markdown + metadata incl. current `version` |
| `get_page_children` | read | site, space_key, page_id | titles + ids, one level |
| `get_comments` | read | site, space_key, page_id | footer + inline threads, with author + selection |
| `search` | read | site, space_key, cql_text, max_results≤25 | broker composes `space = "<space_key>" AND (<cql_text>)` — the pin is server-side, not model-supplied |
| `list_spaces` | read | site | **onboarded+allowed spaces only** (discovery never leaks names) |
| `get_space` | read | site, space_key | homepage id, description |
| `create_page` | write | site, space_key, title, body_markdown (+parent_id) | auto-labels `sdlc-agents-managed`; version message stamped |
| `update_page` | write | site, space_key, page_id, base_version, body_markdown (+title) | **optimistic concurrency**: current ≠ `base_version` ⇒ "page changed since you read it — re-read and retry" |
| `add_comment` | write | site, space_key, page_id, body_markdown (+parent_comment_id) | the reply/propose channel |
| `add_label` | write | site, space_key, page_id, label | label shape-validated |

Absent by construction: delete/archive/restore, page moves, attachment writes, space/permission/user admin. Bodies cross the boundary as **Markdown**; the broker owns Markdown ↔ storage-format conversion (macro-free subset, all output XML-escaped) so the model never authors raw storage XHTML — no macro/XML injection surface (T-54). `tool_definitions()` / pin-test / manifest-check as §B2.2.

### C2.3 Cedar grants

`CONFLUENCE_TARGET = "ConfluenceTarget"`, `CONFLUENCE_TOOL_CLASS`, catalog extension, and `AGENT_TOOL_GRANTS`:

| Agent | Confluence grants | Rationale |
|---|---|---|
| `docwriter` | all reads + `create_page, update_page, add_comment, add_label` | owns the documentation surface |
| `workitems` | all reads + `add_comment` | grounds plans in specs; reports in threads |
| `adr` | all reads + `add_comment, add_label` | links decisions; tags pages, never edits them |
| `researcher` | all reads | context only |

Fleet forbid `sdlc_allowed_spaces` (write tools forbidden `unless { context.input has space_key && (space_key == "DOCS" || …) }`), rendered from allowed space rows, empty ⇒ `unless { false }`; synced under the existing rollback invariant. Advisory `cedar/confluence.cedar` + `shared.cedar` forbids. Interceptor clause: a Confluence-originated dispatch may act on its own space (+ its `repos` for GitHub tools); a GitHub/Slack/Asana-originated dispatch may act on spaces whose `repos` include the origin scope. Prompt rules: ALWAYS `get_page` before `update_page` and pass its version as `base_version`; respect the dispatch block's write mode; signature line on comments and version messages; label created pages `sdlc-agents-managed`.

### C2.4 Reads are scoped too (unlike GitHub)

GitHub's Cedar forbid covers only writes because the per-call App token is already repo-scoped. An Atlassian token is site-wide, so **the Confluence broker enforces the space allowlist on every call including reads**, `search` is server-side space-pinned, and `list_spaces` is filtered. Consequence for admins, stated in the UI at onboarding time: onboarding a space grants fleet-wide *read* visibility (writes stay separately gated). A confidential space you never onboard is invisible to every agent, full stop. (Jira reads are container-checked by the broker too, but tickets don't carry the same confidentiality-by-space convention — the Cedar forbid covers writes, matching GitHub's posture.)

## C3. Write safety — keeping agent-maintained docs trustworthy

### C3.1 Layered invariants

(1) No destructive capability exists (schema absence). (2) Every write is versioned + attributed — page history is the undo button; version messages + signatures name agent and run; `sdlc-agents-managed` makes agent-touched content one CQL query. (3) No stale-write clobbering (`base_version`). (4) Space allowlist at Cedar + broker. (5) Write mode + write agents per space.

### C3.2 Per-space write mode: `direct` vs `propose` (default `propose`)

The fleet's convention is propose → human-approve → execute; Confluence has no PR equivalent, so the space row carries the policy:

- **`propose`** (default): `create_page`/`update_page` rejected by the broker with an instructive error; the agent posts an `add_comment` carrying the proposed content (rendered Markdown + a dashboard run deep-link). A human applies it, or replies `@SDLC Agents docwriter apply it` — the follow-up still runs under `propose` unless an admin has flipped the space, so "apply" means *the human edits, or the admin promotes the space*.
- **`direct`**: writes land immediately (version-guarded, attributed). For agent-owned spaces (release notes, generated API docs) and teams that trust the loop — the version history is the review.

Broker-enforced (Cedar can't read space rows); flipping a space is one admin toggle, no policy deploy. **`write_agents`** optionally narrows write tools in a space to listed agents even when others hold Cedar write grants — the space × agent cell of §A4.

### C3.3 Docs ↔ code freshness

`sdlc-agents-managed` + per-page source labels + the space's `repos` edge let docwriter's `check_doc_freshness` custom tool sweep: for each managed page in mapped spaces, compare the page-version date against the linked repo's recent changes; open a proposal (comment or doc PR) when stale. Prose-level convention + one custom-tool extension — no new infrastructure.

## C4. Traceability

`derive_trace_refs`: `source=="confluence"` emits `confluence_page`/`confluence_space`/`confluence_site` (and the `jira_key` regex still scans the instruction — a page comment naming ENG-142 joins the Jira dimension). `TRACE_DIMENSIONS` gains `confluence_page`; `format.sourceLink` → `<site_url>/wiki/spaces/<KEY>/pages/<id>`. Page → work: result comments link the PRs/issues/runs produced; doc PRs created from a Confluence dispatch carry the page URL in the PR body, and Confluence page URLs in GitHub bodies are regex-scanned into `confluence_page` refs (mirroring `jira_key`). Runtime enrichment unchanged — one trace query shows page → run → PR.

---

# Infrastructure summary (`infra/foundation/template.yaml`)

- **`JiraWebhookFunction`** + **`ConfluenceWebhookFunction`** (mirror `SlackWebhookFunction`): routes `POST /jira/webhook/{site}` / `POST /confluence/webhook/{site}` on `WebhookApi`; Timeout 30, ReservedConcurrentExecutions 10, DLQ; env `REGISTRY_PARAM`, `DISPATCH_FUNCTION`, `FLEET_CONFIG_TABLE`, `ASSIGNMENTS_TABLE`, `STAGE`, `FORGE_JWKS_URL`; SSM **read** on `/sdlc-agents/${Stage}/atlassian/*` (API token, for context fetches); **no `ssm:PutParameter`**; outbound HTTPS to Atlassian's JWKS; `lambda:InvokeFunction` on the router. Always deployed, inert-until-onboarded.
- **`JiraBrokerFunction`** + **`ConfluenceBrokerFunction`** (mirror `ScmBrokerFunction`): SSM read on the atlassian path; config-table read (site/container rows). No `PutParameter`.
- **`JiraGatewayTarget`** / **`ConfluenceGatewayTarget`** (`AWS::BedrockAgentCore::GatewayTarget`): `Name: JiraTarget`/`ConfluenceTarget`, `CredentialProviderConfigurations: [GATEWAY_IAM_ROLE]`, `TargetConfiguration.Mcp.Lambda` → broker ARN + `ToolSchema.InlinePayload` from `tool_definitions()`. Gated `DeployJiraTarget`/`DeployConfluenceTarget` (default false) only because the gateway itself is `DeployGateway`-gated; flip after grants merge. `FleetGatewayRole` gains identity-based invoke on both brokers (the documented CREATE requirement).
- **Forge forwarder app** (`forge/atlassian-events/`, outside the SAM stack): manifest (both products' triggers + remote-endpoint egress) + per-product modules; deployed via `scripts/deploy_forge_atlassian.py`; per-site install via private link.
- **Interceptor**: Atlassian clauses — same function, no new resource. **Token-expiry check**: small scheduled rule (or fold into the weekly rebuild Lambda's schedule pattern) → `credential_expired` notify.
- **Alarms**: `jira-webhook-errors-<Stage>`, `confluence-webhook-errors-<Stage>`, `jira-broker-errors-<Stage>`, `confluence-broker-errors-<Stage>`, `AutomationRuleThrottled`.
- **Outputs**: `JiraWebhookEndpoint`, `ConfluenceWebhookEndpoint` (wired into the Forge manifest at deploy time).
- **Router / `AssignmentNotifierFunction`**: env + `ssm:GetParameter` for the atlassian api-token path (acks/rejects/replies via the two `post_*_comment` functions).

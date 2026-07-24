# Connector: Confluence
## First-class Confluence source — deep documentation context for agents, agent-maintained docs, comment-mention dispatch, and space-scoped fine-grained access control

> **Status: PROPOSED.** This spec onboards **Confluence Cloud** as a first-class fleet connector at parity with GitHub/Asana/Slack (and alongside the proposed Jira connector — see `jira-connector-spec.md`, which explicitly reserved this seam: *"a Confluence connector would be its own connector page over the same site record"*). It covers five capability pillars:
>
> 1. **Confluence as a context source** — every agent that needs it can read pages, page trees, comments, and CQL search results from **onboarded spaces**, through the AgentCore Gateway (Cedar-scoped, broker-enforced), so runs are grounded in the team's actual documentation (§8).
> 2. **Agent-maintained documentation** — *specific* agents (docwriter first) may create and update pages to keep documentation current, under layered write safety: per-agent Cedar grants, a space allowlist, per-space `direct` vs `propose` write modes, optimistic-concurrency version guards, and structurally-absent deletes (§8–§9).
> 3. **Confluence as a dispatch source** — a user @mentions the fleet in a **page or inline comment**; the agent does the work; the reply lands back **in the same comment thread** — the tagging-triggers-work UX GitHub, Asana, and Slack already have (§3, §6, §7).
> 4. **Fine-grained access control** — WHO may trigger (AVP trigger rules), WHERE triggers are honored (space allow/deny), WHICH agent may call WHICH tool (gateway Cedar permits), WHERE writes may land (space-allowlist forbid + broker), and HOW writes land (per-space write mode). Summarized in §2.1 (§5, §8).
> 5. **Traceability, automation & notifications** — native `confluence_page`/`confluence_space` trace refs, agent-stamped version messages and labels, label/page-event automation rules (reusing the Jira spec's source-agnostic engine), and Confluence events in the channel-notification catalog (§10–§12).
>
> Plus a **first-class onboarding experience**: a Connectors → Confluence admin page with a guided connect flow, space onboarding, access rules + simulator, and a `sdlc-agents-connect-confluence` quickstart skill (§13–§16).
>
> Prior art built on, not duplicated: the Slack connector spec (§4/§5 trigger-authz data model, §16–§18 identity/groups/notifications — all BUILT), the Jira connector spec (PROPOSED; site/credential pattern, broker pattern, automation engine — cross-referenced where shared), `scm_broker.py`/`scm_interceptor.py` (the enforced broker/interceptor idiom), and the roadmap note that Atlassian's remote MCP covers "Jira, **Confluence**, Compass under one OAuth" (rejected as the tool path — §8.1).

---

## 1. Goals & non-goals

### Goals

1. **Deep context.** Agents answer with the team's documented truth: architecture pages, runbooks, product specs, decision history. Reads are first-class for **all** agents (Cedar read grants), scoped to onboarded spaces — a confidential space that isn't onboarded is invisible to every agent, reads included.
2. **Consistent, current documentation.** Docwriter (and admin-granted custom agents) create and update pages — release notes published to the team space, API docs synced from code changes, freshness sweeps over agent-managed pages. Every agent write is attributable (version message + label), versioned (Confluence history = built-in undo), and reversible; agents can never delete or archive anything.
3. **Tagging triggers work.** `@SDLC Agents docwriter update this section for the v2 API` in an inline comment dispatches docwriter with the page, the highlighted text, and the comment thread as context; the result returns as a threaded reply. Page comments work the same way. This is the fleet's standard mention UX on a new surface.
4. **Fine-grained access control on two axes** — the explicit requirement this spec centers on:
   - **Space scoping**: which spaces the fleet may read, trigger from, and write to — allow/deny rows + posture per site, enforced at trigger-authz (WHERE), Cedar (write forbid), and the broker (all calls, reads included).
   - **Tool actions**: which agent may call which tool — per-agent Cedar permits from the same `AGENT_TOOL_GRANTS`/`tool_grants` mechanism the dashboard already exposes, read/write classed for the custom-agent authoring UI.
5. **First-class connector-suite membership.** A Connectors → Confluence page with guided connect, per-connector access rules + the Test-access simulator, activity, notifications — the same chrome as Slack/GitHub/Asana. Multi-site from day one.
6. **No regressions.** GitHub/Asana/Slack dispatch and the existing suites keep passing. The Confluence receiver is always deployed but **inert until a site is onboarded** (the Slack §19 posture — fail-closed at runtime, enabled via Admin; no deploy flag).

### Non-goals

- **Confluence Server / Data Center.** Cloud only (REST v2, Atlassian account ids). DC has different auth, webhooks, and user ids.
- **Personal spaces** (`~<accountId>` keys). The space-key validator rejects them in v1; onboarding is for team spaces. Revisit on demand.
- **Attachments, whiteboards, databases, Smart Links** as write targets. Read of page bodies + comments only in v1; attachment upload is a fast-follow candidate on the curated schema.
- **Space administration** (permissions, space creation, themes). Structurally absent from the tool schema.
- **Replacing repo docs.** Doc PRs into `docs/` remain docwriter's primary code-adjacent path; Confluence is the *team/product* documentation surface. The two link to each other (§10).
- **Jira.** Shared Atlassian patterns are cross-referenced, but this spec stands alone — neither connector depends on the other landing first (shared pieces are called out in §17's phase notes).

---

## 2. Where this fits in the existing architecture

Everything reuses an existing seam. The only genuinely new *mechanism* is the Forge event-forwarder app (§6.1) — a small Atlassian-hosted app whose only job is pushing product events to the fleet's receiver, verified per-delivery against Atlassian's published signing keys (no shared secret to capture or store).

| Piece | Existing seam reused | New for Confluence |
|---|---|---|
| Webhook receiver | Thin adapter over `infra/dispatch/mentions.py` + async `Event`-invoke of the router — same as `github_webhook.py` / `asana_webhook.py` / `slack_webhook.py` | `infra/dispatch/confluence_webhook.py`, route `POST /confluence/webhook/{site}`, fed by the Forge forwarder (§6.1) |
| Trigger authz | AVP `TriggerPolicyStore` fixed 3-policy set; grants as `trigger_rule` data — **zero Cedar changes** | principal prefix `confluence:<accountId>`; connector enum `"confluence"`; space WHERE-axis rows (§5.3) |
| Identity | `infra/dispatch/identity.py` get-or-create + first-touch onboarding gate | `handles.confluence = <accountId>`; verified email from Atlassian directory when privacy settings allow |
| Agent tools | AgentCore Gateway Lambda target + broker (`GitHubTarget`/`scm_broker.py` pattern), `AGENT_TOOL_GRANTS` in `fleet_policy.py`, `policy_sync.py` | `ConfluenceTarget` + `infra/dispatch/confluence_broker.py` + `CONFLUENCE_TOOL_CLASS` (§8) |
| Replies | `infra/dispatch/reply.py` + `router._post_block_reply` branch | `post_confluence_comment(site_id, page_id, body, parent_comment_id=None)` |
| Trace refs | `enrichment.derive_trace_refs` open map | native `source == "confluence"` branch (§10) |
| Automation | `automation_rule#` engine (Jira spec §10 — source-agnostic schema) | Confluence events + facts (`page_labeled`, `page_created`, `page_updated`) (§11) |
| Notifications | `notify.py` fan-out, `notif_sub#` rows, `assignment_notifier.py` | Confluence events in `TIER_EVENTS`; optional `spaces` sub scope (§12) |
| Admin UI | `dashboard/src/connectors/registry.ts` descriptor + page; `TriggerRulesPanel`, `ActivityPanel`, simulator | `ConfluenceConnectorPage.tsx` (§13) |
| Admin API | `admin.py` `_route` + `config_store` kinds via `kind-index` GSI | `/admin/confluence/*` (§14) |

**Enum/branch points that must learn `"confluence"`** (the exhaustive checklist, same survey as the Jira spec §2): `config_store.TRIGGER_CONNECTORS` + `IDENTITY_SOURCES` (mirrored in `identity.py`), `router.namespaced_principal` (+ `_identity_source_and_handle`), `router._post_block_reply`, `enrichment.derive_trace_refs`/`derive_participants`, `assignment_notifier._actor_from`, `reply.py`, `agents/shared/dispatch_context.py` (new `confluence_dispatch_block`) + the `agents/_base/agent.py` source switch, `dashboard/src/types.ts` (`TriggerRule.connector`, `Identity.handles`), `dashboard/src/connectors/registry.ts` (id union + entry), `dashboard/src/format.ts::sourceLink` (build `<site_url>/wiki/spaces/<KEY>/pages/<id>` from trace refs), `dashboard/src/App.tsx` (route `#/admin/connectors/confluence`), `infra/foundation/template.yaml` (functions, routes, alarms, target).

### 2.1 The fine-grained access-control matrix (the heart of the spec)

Six independent, composing controls. Each is enforced at a different layer, so no single misconfiguration opens the whole surface:

| # | Question | Mechanism | Layer | Granularity |
|---|----------|-----------|-------|-------------|
| 1 | Who may trigger which agent from Confluence? | `trigger_rule` rows (WHO) → AVP fixed policies | Dispatch Router (AVP) | user / permission-group × agent × site |
| 2 | Which spaces may triggers come from? | `confluence_space#` allow/deny rows + site posture → `context.channelAllowed` (P3 forbid) | Dispatch Router (AVP) | space |
| 3 | Which agent may call which tool? | `AGENT_TOOL_GRANTS` (built-ins) / capability `tool_grants` (custom) → `sdlc_permit_<agent>` Cedar permits | Gateway policy engine | agent × individual tool |
| 4 | Which spaces may agents touch at all (reads included)? | onboarded-space allowlist in the broker (every call), plus the `sdlc_allowed_spaces` Cedar forbid on write tools | Broker + Gateway Cedar | space |
| 5 | May this dispatch touch this space? (origin pinning) | interceptor Confluence clause: origin space / co-scoped repos | Gateway interceptor | dispatch origin × space |
| 6 | How do writes land in this space? | per-space `write_mode: direct \| propose` + optional `write_agents` allowlist | Broker | space × agent × write style |

Layers 1–2 are data-driven (DynamoDB rows — granting is a write, never a Cedar deploy). Layer 3 is the existing per-agent grant surface, already exposed in the custom-agent authoring UI via `tool_catalog()`. Layers 4–6 exist because a Confluence API token is **site-wide** — the credential cannot express space scope, so the broker is the enforcement point (the exact rationale the Jira spec gives for projects, and `scm_broker` gives for co-repo modes).

---

## 3. End-to-end flows

### 3.1 Happy path (inline-comment mention — the doc-work flow)

```
User highlights a stale paragraph on "Payments API Guide" (space DOCS) and comments:
  @SDLC Agents docwriter update this section — we moved to idempotency keys in v2
  │
  ▼  POST /confluence/webhook/{site}   (API Gateway → confluence-webhook Lambda)
confluence_webhook.handler
  1. resolve {site} path param → confluence_site row (onboarded? enabled?)
  2. verify the Forge Invocation Token (RS256 vs Atlassian's JWKS: signature, expiry,
     audience, app id) + cloud-id ↔ site-row cross-check; reject bad/missing/expired
  3. dedup on event id; drop events authored by the fleet service account (bot-loop)
  4. event = comment_created → fetch the comment body (ADF) → scan for the bot account-id
     mention node → agent id = first registry-resolvable token after the mention
     (mentions.resolve_mention fallback on flattened text)
  5. sender = "confluence:<accountId>"; email + displayName from the Atlassian directory
     (authenticated ⇒ email_verified=True when present)
  6. context = {site, space_key:"DOCS", page_id, page_title, comment_id, parent_comment_id,
                inline_selection: "<the highlighted text>", requester_email?,
                comment_thread:[…full thread, flattened…]}
  7. async Event-invoke dispatch-router; return 200
  │
  ▼
router.handler                                    (all existing machinery, unchanged order)
  8. resolve agent → identity.resolve("confluence", accountId, …) → onboarding gate
  9. trigger_authz.is_authorized(principal="confluence:<accountId>", agent="docwriter",
       context={source:"confluence", workspace:<site>, channel:"DOCS", channelAllowed:space_ok})
 10. concurrency, guardrail (unchanged)
 11. create_assignment (trace_refs: confluence_page/confluence_space/confluence_site),
     invoke_agent — source_context verbatim, incl. the inline selection
 12. reply.post_confluence_comment(site, page_id, "🏁 @docwriter is on it — run <id>",
                                   parent_comment_id=comment_id)
  │
  ▼
agent runtime → gateway ConfluenceTarget tools:
     get_page (current body) → update_page (base_version-guarded, version message
     "🤖 [Docwriter Agent] · run <id>") → add_comment reply summarizing the change
     — or, in a propose-mode space, a reply comment carrying the proposed text instead (§9.2)
```

### 3.2 Context-read path (no Confluence trigger at all)

A GitHub-dispatched run (`@researcher analyze the auth options for #142`) calls `ConfluenceTarget___search` (CQL, space-scoped) and `get_page` on onboarded spaces to ground its analysis in the team's existing decisions and runbooks. Nothing Confluence-side fires; the gateway permit + broker space allowlist + interceptor origin check are the whole story. This is pillar 1 working with zero new dispatch machinery.

### 3.3 Automation path ("page labeled → agent runs")

```
User adds label "sdlc-review" to a draft architecture page in space ENG
  ▼  webhook label event → no mention → automation match:
     automation_rules.match(connector="confluence", event="page_labeled",
       facts={site, space:"ENG", label:"sdlc-review", page_id, title})
  ▼  rule hit → cooldown check → dispatch: agent from the rule,
     instruction = rendered template ("Review {{title}} and comment with feedback"),
     sender = "automation:confluence:<rule_id>", trigger_type = "automation"
  ▼  router: SAME spine — the rule's auto-authored trigger_rule grant, guardrail,
     concurrency, auditable assignment (Jira spec §10 semantics, verbatim)
```

### 3.4 Reject path

Identical discipline to Slack §3.2 / Jira §3.4: a non-ALLOW decision records a `blocked_authz` assignment, emits `TriggerDenied` `{source:"confluence", agent, site, reason}`, and posts a **specific** threaded reason:

> ⛔ You aren't authorized to trigger `@docwriter` in this space. Reason: **space not onboarded**. Ask an admin, or check assignment `<id>` in the dashboard.

First-touch users get the standard onboarding reply (org-copy variant — Atlassian reliably supplies a verified email for most sites).

---

## 4. Data model (`infra/dashboard/config_store.py`)

New record kinds in `fleet-config-${Stage}`, `pk`-prefix + `kind-index` GSI convention, ids validated at the store boundary (Cedar-metachar safe): site id `^[0-9a-f-]{36}$` (Atlassian cloud id) or `^[a-z0-9-]+$` (slug), space key `^[A-Z][A-Z0-9]{0,254}$` (**rejects `~` — personal spaces out of scope**), page/comment ids `^[0-9]{1,20}$`, account id `^[0-9a-z:-]{1,128}$`.

### 4.1 Confluence site — `pk="confluence_site#<site_id>"`

```jsonc
{
  kind: "confluence_site",
  site_id: "<cloud_id>",              // Atlassian cloud id (uuid)
  site_url: "https://acme.atlassian.net",
  site_name: "Acme",
  enabled: true,
  bot_account_id: "712020:abc…",      // fleet service account — mention anchor + loop guard
  bot_email: "sdlc-agents@acme.com",
  api_token_param: "/sdlc-agents/<stage>/confluence/<site_id>/api-token",  // SSM SecureString
  forge_app_id: "ari:cloud:ecosystem::app/…",   // pinned at install verification (§6.1) — no webhook secret exists
  webhook_last_seen_at: <epoch|null>,           // liveness, stamped by the receiver (§13 Verify delivery)
  token_expires_at: <epoch|null>,     // API tokens expire (max 1 yr) → credential_expired notify
  default_space_policy: "allowlist" | "denylist",   // WHERE posture, mirrors Slack channels / Jira projects
  onboarded_by, onboarded_at,
  status: "pending" | "active" | "disabled"
}
```

Secrets per site, SecureString, fetched per-invocation (T-8/T-36 discipline). Multi-site = multiple rows; the receiver selects secrets by the `{site}` path parameter. **If the Jira connector is also onboarded on the same Atlassian site**, the service account and API token MAY be shared: the connect flow offers "reuse the Jira site credential" and stores a pointer to the same SSM path — one Atlassian identity (`@SDLC Agents`), two connectors, and the identity map joins users on the same `accountId`/verified email automatically.

### 4.2 Space policy — `pk="confluence_space#<site_id>#<KEY>"`

The WHERE axis (trigger side) **and** the write-safety axis (tool side) — exactly parallel to `slack_chan#`/`jira_proj#` rows, plus the two Confluence-specific fields:

```jsonc
{
  kind: "confluence_space",
  site_id, space_key: "DOCS",
  space_name: "Product Docs",
  mode: "allow" | "deny",
  write_mode: "direct" | "propose",   // §9.2 — how agent writes land in this space (default "propose")
  write_agents: ["docwriter"],        // optional narrowing; [] = any Cedar-granted agent (§9.3)
  repos: ["owner/repo", …],           // the space's linked-repo scope (co-scope edge, mirrors slack_channel.repos)
  note, created_by, created_at
}
```

Interpreted against the site's `default_space_policy` (allowlist recommended for production — and for Confluence it is the **read-confidentiality boundary** too, §8.4, so allowlist is the strong default). `trigger_grants.channel_allowed` generalizes exactly as the Jira spec's `project_allowed`: for `source=="confluence"` the "channel" slot carries the space key, so **Cedar policy P3 needs no change**.

### 4.3 Extensions to existing kinds

- `trigger_rule.connector` gains `"confluence"` (`TRIGGER_CONNECTORS += ("confluence",)`).
- `identity.handles` gains `confluence: "<accountId>"` (+ `handle_keys` entry `confluence:<accountId>`); `IDENTITY_SOURCES += ("confluence",)`. Note: Atlassian account ids are global — a person onboarded via Jira and later touching Confluence carries the same accountId, and the verified-email auto-merge (spec §16.5) unifies the records even if the handles were created independently. (Whether to collapse `jira:`/`confluence:` into one `atlassian:` handle namespace is an open decision, §19.)
- `notif_sub` gains an optional `spaces: ["DOCS", …]` scope alongside `repos`/`projects`, validated ⊆ onboarded spaces (the §18.2/T-43 discipline).
- `automation_rule.connector` gains `"confluence"`; `match` keys for Confluence events: `site`, `space`, `label`, `title_contains` (§11).
- Capability rows: nothing new — custom agents receive Confluence tools through the existing `tool_grants` list, validated by `classify_tool` like every other target's tools.

### 4.4 New `config_store` functions

Mirroring the existing style, each paged + id-validated:
`list_confluence_sites / get_confluence_site / put_confluence_site / set_confluence_site_status / delete_confluence_site`; `list_confluence_spaces(site_id) / put_confluence_space / delete_confluence_space`.

---

## 5. Identity, credentials, and trigger authorization

### 5.1 Principals

- **Human:** `confluence:<accountId>` — Atlassian account ids are immutable (T-4 rationale; never the display name). Applied centrally in `router.namespaced_principal`.
- **Automation:** `automation:confluence:<rule_id>` — synthetic per-rule principal (Jira spec §10.4 semantics).
- **Identity enrichment:** the fetched comment's `author` supplies `accountId`, `displayName`, and (when site privacy settings allow) `emailAddress` from Atlassian's authenticated directory ⇒ seeds the identity map **verified** (`email_verified=True`), joining the person to their GitHub/Slack/Asana/Jira handles (T-42 discipline). Email hidden ⇒ email-less identity, standard §16.5 merge paths.

### 5.2 Site credentials — service account + API token

Same recommendation and rationale as the Jira spec §5.2, and the same one-step paste-token connect UX:

- **A dedicated Atlassian service account** (`sdlc-agents@acme.com`, display name "SDLC Agents") with a **scoped API token** (Basic auth to the Confluence REST API). It is the mention anchor (`@SDLC Agents` is real in Confluence's mention picker), the exact bot-loop filter (`author.accountId == bot_account_id`), and the attribution identity for every page version and comment the fleet writes.
- **Expiry is managed**: `token_expires_at` captured at connect; the existing `credential_expired` error-tier notification fires 14/3/0 days out; countdown badge on the connector page.
- Comment/version attribution: all writes post as the service account; each carries the agent's signature line (`🤖 **[Docwriter Agent]** · run <assignment_id>`) in the comment body or page-version message — per-agent attribution without per-agent credentials.
- OAuth 3LO remains the documented alternative for orgs that forbid service accounts (same caveats and non-schedule as Jira spec §5.2).

### 5.3 Trigger authorization — zero new Cedar

The AVP `TriggerPolicyStore` fixed 3-policy set is untouched:

- **WHO** — `trigger_rule` rows with `connector:"confluence"`; subjects are `confluence:<accountId>` principals, **permission groups** (a group grant authored once already applies here — the §17 identity-map payoff), or automation principals.
- **WHERE** — `context.channelAllowed` computed by `trigger_grants.space_allowed(site_id, space_key)` (sibling of `channel_allowed`, same posture math over `confluence_space#` rows + the site posture; shares the generalized helper with Jira's `project_allowed` if both land). `context.workspace` = site id; `context.channel` = space key. Unknown/disabled site ⇒ False (fail-closed).
- **Fail-closed invariants** unchanged: unresolved sender rejected pre-AVP; grant-read failure ⇒ `authz-unavailable`; deny ⇒ `blocked_authz` row + `TriggerDenied` metric + threaded reason.

---

## 6. Confluence receiver (`infra/dispatch/confluence_webhook.py`, new)

A thin adapter, `confluence-webhook-<Stage>` Lambda, routes on the shared `WebhookApi`. Always deployed, inert until a site row exists.

### 6.1 Event transport — the shared `atlassian-events` Forge forwarder (the one genuinely new mechanism)

Confluence Cloud has **no admin-registered webhook UI** (unlike Jira's System WebHooks): push events require an installed app. **Forge Remote is the chosen transport** — a minimal Forge app, in-repo, whose only job is forwarding product events to the fleet's receiver. **The app is shared with the Jira connector** (`jira-connector-spec.md` §6 adopts it as its transport too): one app, one manifest, one install per Atlassian site delivers both products' events — and the Jira connector thereby sheds its manual webhook registration and stored HMAC secret.

- **The app** (`forge/atlassian-events/` in this repo — one manifest + one thin forwarder module per product): Forge **product triggers** subscribe, for Confluence, to `avi:confluence:created:comment`, `avi:confluence:created:page`, `avi:confluence:updated:page`, and the label added/removed events (the Jira module subscribes to its own `avi:jira:*` set — see the Jira spec §6); each invocation forwards the event payload to the fleet's per-product **remote endpoint** — `POST /confluence/webhook/{site}` here, `POST /jira/webhook/{site}` for Jira events (the site id is baked into each installation's environment variable at deploy time, or carried in the payload's `cloudId` and cross-checked — both, belt-and-braces). Scopes: read-only event scopes only, across both products — the app is *event transport only*; all REST reads/writes use the service-account token (§5.2), never app auth. The manifest declares the fleet endpoint as the app's sole permitted external egress — admins see exactly what data flows where before installing. A site that uses only one connector still installs the same app; events for a product with no onboarded site row are dropped at the receiver (inert-until-onboarded, per product).
- **Delivery verification — no shared secret exists.** Every forwarded call carries a **Forge Invocation Token (FIT)**: an asymmetrically-signed JWT verified against **Atlassian's published JWKS** (RS256; keys fetched + cached with TTL, kid-rotation tolerated). The receiver verifies signature, expiry, audience (the fleet endpoint), and the `app.id`/`installation.context` claims, then cross-checks the installation's cloud id against the `{site}` row. Bad/missing/expired token ⇒ 401; JWKS unreachable ⇒ 503 (fail closed, Forge retries). A `verify_forge_invocation_token(...)` helper joins `verify_hmac_sha256`/`verify_slack_signature` in `mentions.py`. **Consequence:** there is no per-site webhook secret to capture, store, or rotate — no lifecycle-callback secret write, no `connect_secret_param`, and no receiver-side `ssm:PutParameter` grant at all (a strict improvement on the Asana handshake posture; T-9 surface deleted rather than scoped).
- **Distribution & install**: the app is deployed once by the fleet operators (`forge deploy`, production environment) under the fleet's Atlassian developer account, then shared via a **private installation link** — the admin clicks install on their site (no development-mode toggle, unlike a private Connect app; this matters for enterprise orgs that prohibit dev mode). The connector page's install card shows the link + the manifest's scopes/egress so the admin knows what they're approving. Manifest changes (new events/scopes) require admin upgrade-consent per site — an acceptable, arguably desirable, governance step.
- **Operational note (the honest cost):** the Forge app is a second deployable outside the SAM stack — versioned in-repo, deployed via the Forge CLI, subject to Atlassian's runtime quotas (comfortably above webhook-forwarding volumes). `scripts/deploy_forge_atlassian.py` wraps deploy + environment wiring so `docs/aws-deploy.md` stays a linear runbook; the cost is amortized across both Atlassian connectors. Sharing adds one coupling to manage: a manifest change for either product triggers admin upgrade-consent on every installed site, and uninstalling the app silences **both** connectors on that site (surfaced by both connector pages' `webhook_last_seen_at` liveness going stale — §13). The fleet-side contract (`{site}` binding, dedup, mention scan, dispatch payload) is transport-agnostic by construction, so this choice is swappable without touching §§7–19.
- **Rejected alternative — Atlassian Connect:** descriptor-served app with per-site sharedSecret lifecycle capture and HS256+qsh per-delivery JWTs. Rejected because (a) new private Connect installs require enabling development mode on the site, which many enterprise orgs prohibit; (b) the lifecycle sharedSecret adds a whole secret-management surface (capture route, SecureString, scoped `ssm:PutParameter`, rogue-install threat) that Forge simply doesn't have; (c) Connect is on Atlassian's deprecation track — new transport code should not start life legacy. Connect remains the documented fallback for orgs that block Forge apps or where the CLI deploy step is unacceptable; the receiver would gain the lifecycle route + `verify_connect_jwt` helper in that case. *(Pre-build verification, per §19: confirm current Connect end-of-support timeline and that Forge product triggers deliver inline-comment creation events; if inline comments aren't covered, the fallback re-enters.)*

### 6.2 Correctness requirements (the parts a naive port gets wrong)

- **Site binding by path.** The `{site}` path parameter selects the site row. Belt-and-braces: cross-check the FIT's installation cloud id (and the payload's `cloudId`) against the row's `site_id`, and the token's app id against the pinned `forge_app_id`; mismatch ⇒ drop + metric (T-37 analogue — an install on the wrong site must not be processed under another site's policy).
- **Bot-loop prevention.** Drop any event whose actor equals the site's `bot_account_id` **before** mention/automation matching — except that agent-authored `comment_created` events feed the notification path only (§12). An agent's own page update can never re-trigger a rule on the same page (see also the automation cooldown, §11).
- **Dedup.** TTL'd `confluence-event#<sha256(event + page_id + comment_id|label + timestamp)>` items in the assignments table (the `slack_event_dedup` shape) — check-before / mark-after-success, fail-open on store errors (duplicate dispatch tolerated; dropped mention not).
- **Mention detection — ADF node first, text fallback.** Webhook payloads don't carry full bodies reliably: on `comment_created`, fetch the comment via REST (`/wiki/api/v2/comments/{id}?body-format=atlas_doc_format`), scan for `{"type":"mention","attrs":{"id":<bot_account_id>}}`; the agent id is the first registry-resolvable token after the mention node in the flattened text (`_adf_to_text` — the same small walker the Jira spec defines; **shared helper**, do not regex the JSON). Fallback: `mentions.resolve_mention` on the flattened text. Unknown mention ⇒ 200 no-op.
- **Inline comments carry the selection.** For inline comments, the REST fetch includes the anchored text (`inlineProperties.originalSelection`); the receiver puts it in `source_context.inline_selection` — this is what makes "update *this section*" actionable.
- **Context front-loading.** `source_context` carries the page snapshot (`page_id`, `page_title`, `space_key`, page version) + the **full comment thread** flattened as `"[<displayName> at <iso>]:\n<text>"` blocks, best-effort — a fetch failure still dispatches base context `{site, space_key, page_id, comment_id}`. Follow-up mentions re-dispatch with the whole thread ⇒ multi-turn conversation on a page works like GitHub issues.
- **Ack discipline.** Verify → dedup → async `Event`-invoke → 200. The "🏁 on it" ack is posted by the **router** (`_post_block_reply` confluence branch), never on the receiver's request path.
- **Automation matching runs only when no mention resolved** (a mention is explicit intent and wins).
- **No `ssm:PutParameter` on the receiver role at all** — the Forge transport has no captured secret (§6.1), and the API token is written only by the admin connect flow. The receiver's sole config write is the best-effort `webhook_last_seen_at` liveness stamp on the site row.

---

## 7. Outbound replies & agent conversation

- **`reply.post_confluence_comment(site_id, page_id, body, parent_comment_id=None) -> bool`** — fetch the site's API token per-invocation; `POST /wiki/api/v2/footer-comments` (or a reply to `parent_comment_id`, covering inline-comment threads) with the body converted from Markdown to storage format via the shared `_markdown_to_storage` helper (§8.2); returns bool, non-fatal + metric on failure (the existing `post_github_comment` contract). Used by the router for acks, guardrail blocks, authz rejects, and onboarding replies.
- **`router._post_block_reply`** gains a `confluence` branch; the router posts the success ack (like Slack/Jira).
- **Agent result round-trip:** agents post their final answer as an ordinary gateway tool call — `ConfluenceTarget___add_comment` — steered by the dispatch block's `Reply to:` line. `agents/shared/dispatch_context.py` gains `confluence_dispatch_block()` rendering site/space/page/title/version, the comment thread, the **inline selection** when present, the space's `write_mode` (so the agent knows up front whether to apply or propose), and the space's linked-repo scope. Gateway-only chokepoint preserved: no dispatch-side result posting for Confluence.
- **Conversation:** each follow-up mention is a fresh dispatch carrying the full thread (§6.2), so propose → human-approve → execute works verbatim in a comment thread: docwriter proposes in a reply, the user answers `@SDLC Agents docwriter apply it`, the second dispatch applies the update.

---

## 8. Gateway target: `ConfluenceTarget` + broker + Cedar

### 8.1 Why a Lambda broker (GitHub-style), not the Atlassian remote MCP

Same verdict as the Jira spec §8.1, for the same three reasons, plus a fourth specific to Confluence:

1. **Curated tool surface** — `InlinePayload` schema means `delete_page`, `delete_comment`, `archive_page`, and every space/permission-admin op are **structurally absent** (the `DESTRUCTIVE_TOOLS` posture: AgentCore rejects Cedar naming undeclared tools, and an undeclared tool simply cannot be called).
2. **Space scoping the credential can't express** — the API token is site-wide; the broker validates every call against the onboarded-space allowlist before any Confluence request.
3. **The DYNAMIC-listing trap** — the remote MCP requires the OAuth credential provider and `ListingMode: DYNAMIC`, which breaks the gateway root `tools/list` and every Cedar policy write (the documented `AsanaTarget` lesson; template ~2811–2831).
4. **Write-mode enforcement** — `propose` vs `direct` (§9.2) is fleet policy the vendor's MCP knows nothing about; only a broker can enforce it.

`infra/dispatch/confluence_broker.py` (`confluence-broker-<Stage>`): dispatches on `bedrockAgentCoreToolName` client context (`ConfluenceTarget___<tool>` → `_tool_name_from_context`); reads the trusted `_dispatch_agent`/`_dispatch_origin` args injected by the interceptor; **every tool requires `site` and `space_key`** (the invariant that lets the interceptor fire — mirroring `test_every_tool_requires_owner_and_repo`); page-level tools also take `page_id`, and the broker **cross-checks the page's actual space against the `space_key` arg** (arg-consistency, fail-closed — an agent cannot reach page X in space B by claiming space A). Checks, in order: known tool → onboarded + allowed space (**reads included**, §8.4) → per-agent Cedar class sanity (defense in depth) → write-mode/write-agents policy for write tools (§9) → REST call with the site token → structured log `{tool, site, space, page, agent, origin, outcome, latency_ms}`.

The **interceptor** (`scm_interceptor.py`) gains a Confluence clause: for `ConfluenceTarget___*` calls it validates the target space against the *dispatch origin's* co-scope — a Confluence-originated dispatch may act on its own space (+ the space's `repos` for GitHub tools); a GitHub-originated dispatch may act on spaces whose `repos` list includes the origin repo (the edge read in reverse); Slack/Asana-originated dispatches follow their existing repo scope through the same edge. Same rationale as co-repo grouping: the runtime role is identical across dispatches, so origin pinning can't live in Cedar. Contract unchanged: transform = exactly `{headers, body}`; reject = JSON-RPC *result* with `isError: true` (never a JSON-RPC error — it would tear down the MCP session).

### 8.2 Curated tool set (v1)

| Tool | Class | Args (required) | Notes |
|---|---|---|---|
| `get_page` | read | site, space_key, page_id | body returned as Markdown (converted from storage) + metadata incl. current `version` |
| `get_page_children` | read | site, space_key, page_id | titles + ids, one level (page-tree navigation) |
| `get_comments` | read | site, space_key, page_id | footer + inline threads, flattened, with author + selection |
| `search` | read | site, space_key, cql_text, max_results≤25 | broker composes final CQL as `space = "<space_key>" AND (<cql_text>)` — the space pin is server-side, not model-supplied |
| `list_spaces` | read | site | **onboarded+allowed spaces only** (the broker filters — discovery never leaks space names) |
| `get_space` | read | site, space_key | homepage id, description |
| `create_page` | write | site, space_key, title, body_markdown (+parent_id) | auto-labels `sdlc-agents-managed`; version message stamped |
| `update_page` | write | site, space_key, page_id, base_version, body_markdown (+title) | **optimistic concurrency**: current version ≠ `base_version` ⇒ error ("page changed since you read it — re-read and retry"); never clobbers a concurrent human edit |
| `add_comment` | write | site, space_key, page_id, body_markdown (+parent_comment_id) | the reply/propose channel |
| `add_label` | write | site, space_key, page_id, label | label shape-validated `^[a-z0-9][a-z0-9-]{0,63}$` |

Absent by construction: any delete/archive/restore, page moves, attachment writes, space/permission admin, user admin. Bodies cross the boundary as **Markdown**; the broker owns the Markdown ↔ storage-format conversion (`_markdown_to_storage` / `_storage_to_markdown`, macro-free subset, all output XML-escaped) so the model never authors raw storage XHTML — no macro/XML injection surface (T-56). `confluence_broker.tool_definitions()` generates the InlinePayload; a pin-test keeps the template copy in sync (the `scm_broker` regeneration pattern); `scripts/check_gateway_manifest.py` coverage extends to `ConfluenceTarget` before ENFORCE.

### 8.3 Cedar grants — who reads, who writes

`infra/dashboard/fleet_policy.py`: add `CONFLUENCE_TARGET = "ConfluenceTarget"`, `CONFLUENCE_TOOL_CLASS` (the table above, Asana-pattern per-tool class map), extend `classify_tool()`/`tool_catalog()` (so dashboard-authored custom agents can be granted Confluence tools per class through the existing `tool_grants` UI), and extend `AGENT_TOOL_GRANTS`:

| Agent | Confluence grants | Rationale |
|---|---|---|
| `docwriter` | all reads + `create_page, update_page, add_comment, add_label` | owns the documentation surface |
| `workitems` | all reads + `add_comment` | grounds plans in specs; reports back in threads |
| `adr` | all reads + `add_comment, add_label` | links decisions; tags pages, never edits them |
| `researcher` | all reads | context only |

Two new fleet policies, mirroring `sdlc_allowed_repos` exactly:

```cedar
// sdlc_allowed_spaces — writes forbidden outside onboarded+allowed spaces
forbid(
  principal,
  action in [
    AgentCore::Action::"ConfluenceTarget___create_page",
    AgentCore::Action::"ConfluenceTarget___update_page",
    AgentCore::Action::"ConfluenceTarget___add_comment",
    AgentCore::Action::"ConfluenceTarget___add_label"
  ],
  resource == AgentCore::Gateway::"<gateway_arn>"
) unless {
  context.input has space_key &&
  (context.input.space_key == "DOCS" || context.input.space_key == "ENG" /* … rendered from rows */)
};
```

Rendered from the allowed `confluence_space#` rows by `render_fleet_policies` (empty allowlist ⇒ `unless { false }` — all writes forbidden, the safe default); synced by the existing `policy_sync.sync_fleet_policy()` on every space-row change, under the existing rollback invariant. `policy_sync._available_target_names()` already tolerates granting before the target deploys — grants can merge ahead of the `DeployConfluenceTarget` flip.

Advisory mirror: `cedar/confluence.cedar` (agent permits in the `resource.toolName == "confluence_get_page"` dialect, standard "ENFORCED copy lives in fleet_policy.py" header) + `cedar/shared.cedar` gains the destructive-forbid rows (`confluence_delete_page`, `confluence_delete_comment`, `confluence_archive_page`, `confluence_delete_attachment`) — documentation of intent; they're structurally absent anyway.

Prompt updates: `docwriter/prompts.py` et al. gain the Confluence rules — NEVER delete or archive (can't anyway); ALWAYS `get_page` before `update_page` and pass its version as `base_version`; respect the dispatch block's write mode; signature line on comments and version messages; label created pages `sdlc-agents-managed`; honest-error-reporting applies.

### 8.4 Reads are scoped too (unlike GitHub)

GitHub's Cedar forbid covers only writes because the per-call App token is already repo-scoped — reads self-limit at the credential. A Confluence token is site-wide, so **the broker enforces the space allowlist on every call including reads**, `search` is server-side space-pinned, and `list_spaces` returns only allowed spaces. Consequence for admins: onboarding a space grants fleet-wide *read* visibility of it (writes stay separately gated by grants + write policy) — the connector page says this explicitly at onboarding time. A confidential space you never onboard is invisible to every agent, full stop.

---

## 9. Write safety — keeping agent-maintained docs trustworthy

### 9.1 The layered invariants

1. **No destructive capability exists** (schema absence — §8.2).
2. **Every write is versioned and attributed** — Confluence page history is the undo button; version messages + comment signatures name the agent and run id; `sdlc-agents-managed` labels make agent-touched content auditable via one CQL query.
3. **No stale-write clobbering** — `update_page` requires `base_version` (§8.2).
4. **Space allowlist** at Cedar and broker (§8.3–8.4).
5. **Write mode + write agents** per space (below).

### 9.2 Per-space write mode: `direct` vs `propose` (default `propose`)

The fleet's decomposition convention is propose → human-approve → execute; Confluence has no PR equivalent, so the space row carries the policy:

- **`propose`** (default for newly onboarded spaces): `create_page`/`update_page` are rejected by the broker with an instructive error; the agent instead posts an `add_comment` carrying the proposed content (rendered Markdown + a dashboard run deep-link). A human applies it, or replies `@SDLC Agents docwriter apply it` — the follow-up dispatch still runs under `propose` unless an admin has flipped the space, so "apply" means *the human edits, or the admin promotes the space*. This keeps the human on the page for exactly as long as the team wants.
- **`direct`**: writes land immediately (version-guarded, attributed). Appropriate for agent-owned spaces (release notes, generated API docs) and for teams that trust the loop — the version history is the review.

Broker-enforced (Cedar can't read space rows; the broker reads them via the standard 30s-TTL `fleet_config`-style cache). Flipping a space's mode is one admin toggle, no policy deploy.

### 9.3 Per-space `write_agents` (optional narrowing)

`write_agents: ["docwriter"]` restricts write tools in that space to the listed agents even if others hold Cedar write grants — e.g. a custom agent granted `update_page` for its own team space can't touch the release-notes space. Empty list = no narrowing (Cedar grants govern). Broker-enforced, same cache. This is the space × agent cell of the §2.1 matrix.

---

## 10. Traceability

- **Run → page:** `enrichment.derive_trace_refs` gains a native branch: `source == "confluence"` emits `confluence_page` (page id), `confluence_space`, `confluence_site` (+ the existing `jira_key` regex keeps scanning the instruction text, so a page comment naming ENG-142 still joins the Jira dimension). `derive_participants` emits requester/page-author/thread-commenters with `source:"confluence"`. `trace_refs` is an open map ⇒ no dashboard schema change; `queries.TRACE_DIMENSIONS` gains `confluence_page`; `format.sourceLink` builds `<site_url>/wiki/spaces/<KEY>/pages/<id>` so trace chips are clickable.
- **Page → work:** agents close the loop in-content — the result comment links the PRs/issues/runs produced; doc PRs created from a Confluence dispatch carry the page URL in the PR body (the enrichment regex convention extended: `confluence.net/wiki/...` URLs in GitHub bodies are scanned into `confluence_page` refs, mirroring `jira_key`). Runtime enrichment (`assignment.update_trace_refs`) is unchanged — a Confluence-originated run that produces a PR shows page → run → PR in one trace query.
- **Docs ↔ code freshness:** `sdlc-agents-managed` + per-page source labels (e.g. `src-payments-service`) plus the space's `repos` edge let docwriter's `check_doc_freshness` custom tool sweep: for each managed page in mapped spaces, compare page version date against the linked repo's recent changes and open a proposal (comment or PR) when stale. This is prose-level convention + one custom-tool extension — no new infrastructure.

---

## 11. Event automation ("tagging that triggers work", beyond mentions)

Reuses the **source-agnostic `automation_rule#` engine** specced in `jira-connector-spec.md` §10 (match/cooldown/chain-guard/auto-authored grant/admin-gated). Whichever connector ships first builds the engine; this spec contributes the Confluence event vocabulary:

| Event | Facts | Canonical rule example |
|---|---|---|
| `page_labeled` | site, space, label, page_id, title | label `sdlc-review` added ⇒ run `adr`: "Review {{title}} against the ADR library and comment" |
| `page_created` | site, space, page_id, title, author | new page in `RUNBOOKS` ⇒ run `docwriter`: "Check {{title}} for runbook-template compliance" |
| `page_updated` | site, space, page_id, title, version | *(cooldown-critical — edit storms; default `cooldown_seconds` 3600)* |

Loop safety inherits all three Jira-spec brakes (bot-actor guard, per-(rule,page) cooldown, hourly ceiling + `AutomationRuleThrottled`) — and note the interaction made safe by the first brake: an agent's own `update_page` emits a `page_updated` webhook, but its actor is the service account, so it can never match a rule. Template variables: `{{title}} {{space}} {{page_id}} {{page_url}} {{label}} {{author}}` (allowlisted; rendered instruction passes the edge guardrail like any user text).

---

## 12. Notifications

Additions to the existing catalog (`slack_notify.TIER_EVENTS`), delivered through the built Part-II machinery (channel subs, threading, identity-resolved mentions):

- **actionable:** `doc_proposal_ready` (an agent posted a propose-mode proposal awaiting a human) — mentions the requester.
- **informative:** `page_published` (an agent created/updated a page in `direct` mode), `automation_fired`.
- **error:** `credential_expired` (now also fired by the Confluence token-expiry check), `automation_throttled`.

`notif_sub` rows gain the optional `spaces` scope (§4.3); `notify.notify()` gains the `space=` match axis alongside `repo`/`project`. Threading: `unit = page_id`, so a page's lifecycle collapses into one thread per channel. Emitters: the receiver (agent-comment events for the §3.3-style path), the broker's write path via the assignments stream (agents stay credential-free — the `assignment_notifier` owns terminal events, per the established disjoint-emitters rule). If the Jira spec's per-user DM preferences (`notif_pref#`) land, `doc_proposal_ready` DMs the requester through the same seam with zero Confluence-specific work.

---

## 13. Connectors UI — `dashboard/src/connectors/ConfluenceConnectorPage.tsx`

Registry entry in `registry.ts` (`id: "confluence"` added to the descriptor union; health badge via `useStatus` = site count + token-expiry warnings + webhook last-seen). Route `#/admin/connectors/confluence`. Tabs (Slack page = chrome template; Jira page = closest content template):

- **Sites** — the guided connect (§16): service-account checklist → paste site URL + service-account email + API token → **Connect** (one POST verifies the token against the Confluence user endpoint, resolves `bot_account_id` + cloud id, stores the SecureString, writes the row) → **app-install card** showing the Forge app's private installation link + its manifest scopes/egress → **Verify delivery** button (liveness via the site row's `webhook_last_seen_at`, stamped by the receiver). Per-site enable/disable/remove, token-expiry countdown, "reuse Jira credential" option when a matching `jira_site` row exists.
- **Spaces** — allow/deny rows + posture toggle + per-space **write mode** (`propose`/`direct`), **write agents**, and linked-repo scope (space list fetched live from the site; the onboarding modal states plainly: *"onboarding a space makes it readable by all agents"* — §8.4).
- **Access rules** — `TriggerRulesPanel` filtered to `connector="confluence"` + the **Test access** simulator (subject × agent × site × space → ALLOW/DENY + deciding policy).
- **Automations** — the shared automations tab, filtered to Confluence events (lands with the engine phase).
- **Notifications** — channel subs with space scope (admin view).
- **Activity** — `ActivityPanel source="confluence"` + receiver errors + `TriggerDenied` + `AutomationRuleThrottled`.

`types.ts` additions: `ConfluenceSite`, `ConfluenceSpace`; `TriggerRule.connector` union += `"confluence"`. `api.ts`: `listConfluenceSites / connectConfluenceSite / deleteConfluenceSite`, `listConfluenceSpaces / putConfluenceSpace / deleteConfluenceSpace`, `verifyConfluenceWebhook(siteId)`.

---

## 14. Admin API routes (`infra/dashboard/admin.py`)

All `auth.is_admin`, fail-closed, `_route` pattern:

| Method + path | Purpose |
|---|---|
| `GET/POST /admin/confluence/sites`, `POST /admin/confluence/sites/connect`, `DELETE …/{site_id}` | Site CRUD; `connect` = verify token + resolve cloud id/bot account + store SecureStrings + write row (the Slack/Jira one-step-connect pattern) |
| `POST /admin/confluence/sites/{site_id}/verify-webhook` | Delivery liveness check for the connect flow |
| `GET/POST /admin/confluence/spaces`, `DELETE …/{site_id}/{key}` | Space allow/deny + write mode + write agents + repo scope. **Writes run `_sync_after_write`** — space rows render into `sdlc_allowed_spaces`, so the persist↔project rollback invariant applies (unlike WHO trigger rules, which stay pure data) |
| *(existing)* `/admin/trigger-rules?connector=confluence`, `/admin/trigger-rules/simulate` | WHO rules + simulator — connector param only |

IAM: the admin Lambda's connect route needs `ssm:PutParameter` scoped to `/sdlc-agents/${Stage}/confluence/*` (the Jira-spec §13 deviation, same justification: paste-token *is* the first-class onboarding). No AVP permissions (grants are data; the simulator stays local).

---

## 15. Infrastructure (`infra/foundation/template.yaml`)

- **`ConfluenceWebhookFunction`** (`confluence-webhook-<Stage>`, mirrors `SlackWebhookFunction`): route `POST /confluence/webhook/{site}` on `WebhookApi`; Timeout 30, ReservedConcurrentExecutions 10, DLQ; env `REGISTRY_PARAM`, `DISPATCH_FUNCTION`, `FLEET_CONFIG_TABLE`, `ASSIGNMENTS_TABLE` (dedup items), `STAGE`, `FORGE_JWKS_URL`; SSM **read** on `/sdlc-agents/${Stage}/confluence/*` (API token, for context fetches); **no `ssm:PutParameter`** (§6.1); outbound HTTPS to Atlassian's JWKS endpoint; `lambda:InvokeFunction` on the router. Always deployed, inert-until-onboarded.
- **Forge forwarder app** (`forge/atlassian-events/`, shared with the Jira connector; deployed via `scripts/deploy_forge_atlassian.py` — outside the SAM stack, §6.1): manifest (both products' triggers + remote endpoint egress) + per-product forwarder modules; per-site install via private link.
- **`ConfluenceBrokerFunction`** (`confluence-broker-<Stage>`, mirrors `ScmBrokerFunction`): SSM read on the confluence path; config-table read (site/space rows). No `PutParameter`.
- **`ConfluenceGatewayTarget`** (`AWS::BedrockAgentCore::GatewayTarget`): `Name: ConfluenceTarget`, `CredentialProviderConfigurations: [{CredentialProviderType: GATEWAY_IAM_ROLE}]`, `TargetConfiguration.Mcp.Lambda` → broker ARN + `ToolSchema.InlinePayload` from `tool_definitions()` (§8.2). Gated `DeployConfluenceTarget` (default false) only because the gateway itself is `DeployGateway`-gated; flip after grants merge. `FleetGatewayRole` gains identity-based invoke on the broker (the documented CREATE requirement).
- **Interceptor**: Confluence clause (§8.1) — same function, no new resource.
- **Token-expiry check**: folds into the same scheduled check the Jira spec defines (or ships it, if Confluence lands first).
- **Alarms**: `confluence-webhook-errors-<Stage>`, `confluence-broker-errors-<Stage>`.
- **Outputs**: `ConfluenceWebhookEndpoint` (the value wired into the Forge manifest's remote endpoint at deploy time).
- Router: env + `ssm:GetParameter` for the confluence api-token path (acks/rejects via `post_confluence_comment`); same for `AssignmentNotifierFunction` if stream-driven replies are enabled for Confluence.

---

## 16. First-class onboarding UX

**Skill:** `skills/sdlc-agents-connect-confluence/SKILL.md` (template: `sdlc-agents-connect-asana` — frontmatter with trigger phrasing, two-channel auth explanation, SSM paths, inline verification probes, pitfalls table, `.sdlc-agents/selection.yaml` recording, explicit non-goals). `skills/sdlc-agents/SKILL.md` Step-1 toolchain discovery gains the Confluence connect path. `docs/aws-deploy.md` gains the SSM rows + steps.

**Admin walk-through (~10 minutes):**

1. In Atlassian: create (or reuse the Jira connector's) `sdlc-agents` service account, grant it the target spaces' page permissions, mint a scoped API token.
2. Dashboard → Connectors → Confluence → **Connect a site**: paste site URL + service-account email + token → Connect.
3. The page shows the **app-install card**: install the fleet's Forge event-forwarder app from its private installation link (scopes + egress shown up front; no secret changes hands — deliveries are verified against Atlassian's signing keys) → click **Verify delivery** → green check.
4. **Spaces tab**: onboard `DOCS` (allowlist), leave `write_mode: propose`, link its repos.
5. **Access**: grant a permission group the WHO rules (existing group grants already apply — identity-map payoff); simulator to confirm.
6. Users `@SDLC Agents docwriter …` in a page or inline comment. First-touch users get the standard onboarding reply → admin approves in Access → Users.
7. When trust is established, flip `DOCS` to `write_mode: direct` for the doc-sync loop; optionally author the first automation rule (`sdlc-review` label → adr).

---

## 17. Delivery phases (the plan)

Ordered by the pillar order of value: context reads first, writes second, dispatch third. Each phase independently shippable and test-gated; §2's enum checklist lands with the phase that first needs each entry. *Shared-with-Jira note:* the `forge/atlassian-events/` forwarder app + `verify_forge_invocation_token`, the ADF walker, automation engine, site-credential pattern, and token-expiry check are shared pieces — whichever connector spec is built first implements them; the second consumes (the first build scaffolds the forwarder with both product modules stubbed, so adding the second product is a manifest+module addition, not a new app).

1. **Read path (deep context).** `confluence_broker.py` (read tools only) + `ConfluenceGatewayTarget` + interceptor clause, `fleet_policy.py` read grants + `CONFLUENCE_TOOL_CLASS` + `tool_catalog`, `cedar/confluence.cedar`, minimal site/space rows + store functions + `/admin/confluence/sites|spaces` routes (no UI yet — API-first like the Slack spine). *Exit: any granted agent greps team docs via CQL and reads pages from onboarded spaces; a non-onboarded space is invisible; `check_gateway_manifest.py` passes.*
2. **Write path (agent-maintained docs).** Write tools + Markdown↔storage converter + `base_version` guard, `sdlc_allowed_spaces` forbid rendering + sync, `write_mode`/`write_agents` broker enforcement, docwriter grants + prompt rules + freshness-tool extension. Roll out LOG_ONLY → ENFORCE (existing path). *Exit: docwriter publishes release notes to a `direct` space and files proposals in a `propose` space; researcher write attempts are Cedar-denied; stale-version writes are rejected.*
3. **Dispatch source (tagging triggers work).** Forge forwarder app + deploy script, `verify_forge_invocation_token` (JWKS verify), `confluence_webhook.py` (dedup/bot-loop/mention scan/ADF walker/inline selection/context front-load), `reply.post_confluence_comment`, router branches, identity/enrichment/notifier branches, template resources. *(Pre-phase gate: the §6.1 verification — Forge event coverage for inline comments + Connect timeline.)* *Exit: a mention in a page or inline comment on an onboarded space dispatches; replies land in-thread; rejects and onboarding replies work; existing suites green.*
4. **Onboarding UI.** `ConfluenceConnectorPage` (Sites/Spaces/Access/Activity), connect + verify-webhook routes, registry/types/api/App/format wiring. *Exit: the §16 walk-through works end-to-end with no CLI.*
5. **Automation + notifications.** Confluence events in the automation engine (build engine here if Jira hasn't), `TIER_EVENTS` additions, `notif_sub.spaces`, receiver notify hooks. *Exit: the label rule fires and is throttleable; channels get space-scoped, threaded fan-out.*
6. **Docs, skills, threat model, rollout.** Connect skill, `aws-deploy.md`, threat-model rows (§18), CLAUDE.md connector table row, deploy dev → gamma → prod with `DeployConfluenceTarget=true` after grant merge.

---

## 18. Testing

Mirrors the per-module style of the Slack/Jira suites; every phase lands with its tests.

- **`confluence_webhook`**: valid Forge Invocation Token → correct dispatch payload; bad signature / expired / wrong audience / wrong app id → 401; JWKS unreachable → 503 (fail closed); key-rotation (new kid) → refetch and verify; unknown/disabled site → drop; cloud-id ↔ site-row mismatch → drop + metric; dedup (same event twice → one dispatch; fail-open on store error); bot-actor events never dispatch/match rules but do feed notify; ADF mention resolution + text fallback + unknown mention no-op; inline selection captured; thread front-loaded; context fetch failure still dispatches base context; automation matched only when no mention.
- **`confluence_broker`**: tool dispatch by name; space allowlist on **reads and writes**; page↔space arg-consistency (mismatch → error); `search` CQL space pin (model-supplied `space=` clauses can't widen it); `list_spaces` filtered; `base_version` stale-write rejection; `write_mode: propose` rejects page writes with instructive error but allows `add_comment`; `write_agents` narrowing; per-agent tier fail-closed on missing `_dispatch_agent`; Markdown→storage escaping (no raw XHTML/macro injection); token fetched per-invocation; `tool_definitions` ↔ template pin-test; destructive-tool absence pinned.
- **`fleet_policy`/`policy_sync`**: Confluence grants render; `sdlc_allowed_spaces` renders from rows (`unless {false}` when empty); `classify_tool` on Confluence names; undeployed-target filtering.
- **`trigger_grants`**: `space_allowed` posture math (allowlist/denylist/unknown-site-closed/non-confluence-open).
- **interceptor**: Confluence clause origin pinning (own space allowed; unlinked space rejected via tool-error shape, not JSON-RPC error; body type preserved dict-in/dict-out).
- **`config_store`/`admin`**: new-kind CRUD + id validation (Cedar-metachar, `~` personal-space keys rejected); connect route (token-verify mock, SecureString writes, credential-reuse path, idempotent re-connect); space writes trigger `_sync_after_write` with rollback-on-enforcing semantics; `is_admin` gating throughout.
- **`enrichment`/`reply`/router**: native confluence trace_refs + participants; `post_confluence_comment` bool contract + parent-comment threading; authz-deny posts threaded comment + `blocked_authz` + `TriggerDenied`.
- **SPA**: `#/admin/connectors/confluence` routing; registry descriptor unique/routable; `sourceLink` confluence branch; `npm run build` green.

---

## 19. Threat-model additions (`docs/threat-model.md`)

New components **C-26 Confluence receiver + Forge forwarder app + service account**, **C-27 Confluence broker/target** *(numbered after the Jira spec's reserved C-23–C-25; renumber against whichever spec lands first — same for threats/DFs below, which follow the Jira spec's T-46–T-53 block)*. New threats:

| ID | Threat | Mitigation |
|----|--------|-----------|
| T-54 | Webhook forgery / cross-site event confusion | per-delivery Forge Invocation Token verified RS256 against Atlassian's JWKS (signature, expiry, audience, pinned app id); cloud-id ↔ site-row cross-check; fail-closed when JWKS unavailable. No shared secret exists to steal or replant (§6.1–6.2) |
| T-55 | Site-wide token blast radius (no per-space credential exists) | broker space allowlist on ALL calls incl. reads, server-side CQL space pin, filtered `list_spaces`, Cedar `sdlc_allowed_spaces` write forbid, interceptor origin pinning, curated no-delete schema (§8) |
| T-56 | Storage-format / macro injection via agent-authored bodies | Markdown-only tool boundary; broker-owned conversion, macro-free subset, XML-escaped output (§8.2) |
| T-57 | Prompt injection via page/comment content read as context | pages are untrusted user text (T-1 family): the fail-closed model-attached guardrail covers every agent model call; writes remain gateway-Cedar-scoped regardless of what the model was told (existing T-1/2/3 posture, restated for the new corpus) |
| T-58 | Silent doc corruption at scale (a misbehaving agent rewriting many pages) | default `write_mode: propose`; version history + attribution labels for audit/rollback; `base_version` guard; per-space `write_agents`; concurrency caps; `page_published` notifications make direct-mode writes visible (§9, §12) |
| T-59 | Stale-write clobbering of concurrent human edits | `base_version` optimistic concurrency, broker-rejected with re-read guidance (§8.2) |
| T-60 | Automation loops / edit storms | bot-actor guard (agent writes never match rules), per-(rule,page) cooldown, hourly ceiling + throttle alarm, chain-depth cap (Jira spec §10.5, shared engine) (§11) |
| T-61 | Confidential-space exposure via over-broad onboarding | onboarding = fleet-wide read is stated in the UI at the decision point; allowlist posture recommended; reads logged with space dimension for audit (§8.4, §13) |

Plus DF rows for the Confluence inbound flow (Forge forwarder → receiver → router, FIT-authenticated), the broker outbound flow, and the propose/apply comment loop; a changelog row; risk summary recounted.

---

## 20. Open decisions

1. **~~Connect vs Forge transport~~ — DECIDED: Forge Remote** (§6.1). Rationale: no shared secret to manage (JWKS-verified deliveries), no development-mode requirement for private installs, and no new code on Connect's deprecation track — at the cost of one CLI-deployed artifact outside the SAM stack. Connect remains the documented fallback. **Pre-build verification (Phase-3 gate):** confirm the current Connect end-of-support timeline and that Forge product triggers deliver inline-comment creation events with enough payload to resolve the comment; if inline-comment coverage is missing, the Connect fallback re-enters for the dispatch pillar only (pillars 1–2 don't depend on the transport).
2. **Shared `atlassian:` handle namespace** — if both Jira and Confluence connectors land, collapse `jira:<accountId>` / `confluence:<accountId>` into one handle (account ids are global)? Verified-email auto-merge already unifies the *identity*, so this is cosmetic-plus-simplification; decide when the second connector ships.
3. **Sub-space scoping** (restrict agents to a page subtree via a root `parent_id` on the space row) — the broker could enforce ancestry; deferred until a real need (space-per-concern is the recommended modeling instead).
4. **Attachment upload** (`add_attachment`) for docwriter diagrams — additive to the curated schema; needs a size/type allowlist; fast-follow candidate.
5. **Read-visibility narrowing per agent** (`read_agents` mirroring `write_agents`) — deferred; onboarding granularity (per space) has been sufficient for the analogous repo case.
6. **Confluence page watching → per-user DMs** ("DM me when an agent edits a page I watch") — rides the Jira spec's `notif_pref#` surface if/when it lands; requires the watchers REST read; not scheduled.

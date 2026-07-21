# Connectors: Multi-Workspace Slack, Trigger Authorization, Identity & Notifications
## Admin-managed event sources with per-connector access rules

> **Status: Part I BUILT (behind `DeploySlack`); Part II PROPOSED.**
>
> **Part I (§1–§15, built):** (1) a first-class **Slack** dispatch source at parity with GitHub and Asana, (2) a **Cedar-backed, data-driven trigger-authorization** layer (a third AVP policy store, evaluated by the Dispatch Router) that decides *who* may trigger *which* agent *where*, (3) a **channel onboarding request** flow — users request channel access via the `/sdlc-onboard-channel` slash command and admins approve/deny in the panel (§4.5), and (4) a **Connectors** section **inside the Admin panel** of the dashboard SPA, with a dedicated sub-page per connector (Slack, Asana, GitHub) that owns that connector's connection, triggers, and **per-connector access rules**. It supersedes the Slack sections of `dispatch-agent-assignment-spec.md` §4c. Implemented under `infra/dispatch/` (`slack_webhook.py`, `trigger_authz.py`, `trigger_grants.py`, `reply.py`, `mentions.py`), `infra/dashboard/` (`config_store.py`, `admin.py`), `infra/foundation/template.yaml` (`TriggerPolicyStore` + `SlackWebhookFunction`), `dashboard/src/connectors/`, and `scripts/bootstrap_slack.py`. The Slack receiver is gated by `DeploySlack` (default off); the trigger-authz store is always-on foundation.
>
> **Part II (§16–§20, proposed, not built):** a cross-source **identity map** (email as golden join id, get-or-create on first touch from any source, admin-approved onboarding), **permission groups** (the recommended access mechanism, reusing the existing Cedar group axis), **self-serve interactive Slack notifications** (tiered: actionable / informative / error, via `/sdlc-notify` modals + threading + identity-resolved mentions), and **retirement of the `DeploySlack` deploy gate** in favor of admin workspace-onboarding.

---

## 1. Goals & non-goals

### Goals

1. **Slack as a first-class source.** A user `@mentions` a fleet agent (or runs a slash command) in Slack; the agent does the work; results land back in the **same Slack thread** — the same UX GitHub and Asana already have.
2. **Multiple Slack workspaces.** An admin onboards **one or more** workspaces; each carries its own bot token + signing secret. A dispatch is always evaluated in the context of the workspace it came from.
3. **Cedar-backed trigger authorization.** Admins author rules for *who* may trigger (users / groups), *which* agents, and *where* (which workspace + channels), with **proper allow/deny** behavior and auditable, explainable rejects. Decisions are made by **Amazon Verified Permissions (AVP)** evaluating Cedar — the same mechanism the dashboard API already uses.
4. **Connectors UI inside Admin.** A **Connectors** area within the Admin panel, with a dedicated sub-page per connector. **Access rules are managed per connector** (a Slack rule references Slack channels; it has no meaning on the Asana page).
5. **No regressions.** GitHub and Asana dispatch, and the existing 316-test suite, keep passing. Every new capability is gated behind a deploy toggle and defaults **off**.

### Non-goals

- Slack interactive components (buttons, modals, Block Kit actions) beyond posting threaded messages — a fast-follow.
- *(Not a non-goal — moved into core.)* GitHub/Asana authorization runs on the same Cedar path as Slack from the start. v2 is unreleased, so there is no deployed `authorization.users` behavior to preserve; keeping a parallel flat allowlist would just be legacy under another name. All three sources share one authorization mechanism (§5).
- Automating Slack-side setup (creating usergroups, channels). The admin does Slack-side configuration; the fleet consumes it.

---

## 2. Where this fits in the existing architecture

Three authorization decision points exist or are introduced. Two already exist; this spec adds the third.

| # | Surface | Decides | Mechanism (today / proposed) |
|---|---------|---------|------------------------------|
| A | **Dashboard API** (`infra/dashboard/auth.py`) | May this operator read / this admin write the dashboard? | AVP `DashboardPolicyStore`, static Cedar, Cognito groups as parent entities. **Unchanged.** |
| B | **Tool calls** (`infra/dashboard/fleet_policy.py` + `policy_sync.py`) | May this agent call this tool on this repo? | AgentCore Gateway Cedar engine, policies **dynamically synced** from admin config. **Unchanged.** |
| C | **Trigger** (`infra/dispatch/router.py::authorize_trigger`) | May this sender trigger this agent from this source / workspace / channel? | AVP `TriggerPolicyStore`, Cedar, rules dynamically synced from admin config — the **sole** trigger-authz mechanism for all sources. (Replaces the flat `capability.authorization.users` allowlist, which is removed; v2 is unreleased so there is nothing to deprecate.) |

Decision **C** is the heart of this spec. It reuses pattern **A** (AVP `IsAuthorized`, fail-closed) for the *evaluation* and pattern **B** (config row → dynamically-managed Cedar policy, with a rollback invariant) for the *management*.

The dispatch spine is otherwise reused wholesale. The router already accepts `source: "slack"`, `trigger_type: "slash_command"`, and a `context` with `channel_id`/`thread_ts` (see the `handler` docstring in `infra/dispatch/router.py`); `enrichment.py` already documents `slack` as a valid source; `config_store` already models `triggers{source:[event]}`. So the router changes are surgical (§5), and the bulk of the work is a new receiver, an outbound reply channel, the AVP store, the admin surface, and the UI.

---

## 3. End-to-end flows

### 3.1 Happy path (Slack mention)

```
User in #eng (workspace ACME):  @fleetbot @workitems break this into issues
  │
  ▼  POST /slack/events  (API Gateway → slack-webhook Lambda)
slack_webhook.handler
  1. resolve team_id "T0ACME" → workspace record (enabled?) → its signing secret
  2. verify Slack v0 signature over "v0:{ts}:{raw_body}"; reject replay (>5 min)
  3. dedup on event_id (conditional write); ignore bot_id / self events
  4. strip "<@U0FLEETBOT>" prefix → mentions.resolve_mention → agent_id "workitems"
  5. sender = "slack:T0ACME:U123"; best-effort users.info → email
  6. context = {workspace:"T0ACME", channel_id:"C0ENG", thread_ts, message_ts, requester_email?}
  7. async Event-invoke dispatch-router; return 200 within 3 s
  │
  ▼
router.handler
  8. resolve agent (unchanged)
  9. check_authorization → trigger_authz.is_authorized(
        principal="slack:T0ACME:U123", agent="workitems",
        context={workspace, channel, source})    ← AVP IsAuthorized, fail-closed
 10. check_repo_allowed (no-op for slack), concurrency, guardrail (unchanged)
 11. create_assignment, invoke_agent (unchanged)
 12. reply.post_slack_message(channel, "🏁 @workitems is on it…", thread_ts)
  │
  ▼
agent runtime → does work → post_results (slack-aware) → threaded reply in #eng
```

### 3.2 Reject path (unauthorized / wrong channel)

At step 9 a non-ALLOW decision means the router:
- records a `blocked_authz` assignment row (auditable in the dashboard — not silent),
- emits a `TriggerDenied` CloudWatch metric dimensioned `{source, agent, workspace, reason}`,
- posts a **specific** reason to the thread via `reply.post_slack_message`:
  > ⛔ You aren't authorized to trigger `@workitems` in this channel. Reason: **channel not permitted**. Ask an admin, or check assignment `<id>` in the dashboard.
- returns 403 to its (already-acked) caller.

The reason string is derived from the AVP decision's `determiningPolicies` (§5.4): `unknown-or-disabled-workspace`, `channel-denied`, `user-not-permitted`, `agent-disabled`, `no-matching-rule`.

---

## 4. Data model (`infra/dashboard/config_store.py`)

New record kinds in the single fleet-config table (`FLEET_CONFIG_TABLE`), following the existing `repo#` / `capability#` pk-prefix convention. All new id inputs are validated at the store boundary (like `_AGENT_ID_RE` / `_valid_repo`) so they can't inject Cedar metacharacters or resource-name garbage.

### 4.1 Slack workspace — `pk="slack_ws#<team_id>"`

```jsonc
{
  kind: "slack_workspace",
  team_id: "T0ACME",                 // ^T[A-Z0-9]{6,}$
  team_name: "Acme Corp",
  enabled: true,
  default_channel_policy: "denylist" | "allowlist",  // how Channels tab is interpreted
  signing_secret_param: "/sdlc-agents/<stage>/slack/T0ACME/signing-secret",  // SSM SecureString
  bot_token_param:      "/sdlc-agents/<stage>/slack/T0ACME/bot-token",       // SSM SecureString
  onboarded_by, onboarded_at,
  status: "pending" | "active" | "disabled"
}
```

Secrets are **per workspace**, stored as SSM SecureString (never in the row). This is what makes multi-workspace real: the receiver selects the secret by inbound `team_id`.

### 4.2 Channel policy — `pk="slack_chan#<team_id>#<channel_id>"`

```jsonc
{
  kind: "slack_channel",
  team_id, channel_id: "C0ENG",      // ^C[A-Z0-9]{6,}$
  channel_name: "#eng",
  mode: "allow" | "deny",
  note, created_by, created_at
}
```

Interpretation depends on the workspace's `default_channel_policy`:
- **allowlist** — a trigger is allowed only in channels with an `allow` row (default-deny per channel).
- **denylist** — a trigger is allowed in any channel except those with a `deny` row (default-allow per channel).

Both are expressible in Cedar (§5.2). The **allowlist** posture is recommended for production.

### 4.3 Trigger rule — `pk="trigger_rule#<uuid>"`

The unit an admin creates on a connector's **Access rules** tab — the **WHO** axis only. It is pure data (no `avp_policy_id`; grants aren't projected to per-rule policies). Channel gating ("WHERE") is a *separate* axis carried by the `slack_channel` rows (§4.2), so the two compose without overlap.

```jsonc
{
  kind: "trigger_rule",
  rule_id: "<uuid>",
  connector: "slack" | "asana" | "github",   // which sub-page owns it (per-connector rules)
  subject_type: "user" | "group",
  subject_id: "slack:T0ACME:U123" | "group:eng-oncall",
  agent_id: "workitems" | "*",                // "*" = any agent
  workspace: "T0ACME" | "*",                  // slack only; "*" = any (or absent for non-slack)
  effect: "permit" | "forbid",
  created_by, created_at
}
```

`connector` scopes the rule to exactly one sub-page — satisfying **per-connector rules**. The Slack page lists only `connector=="slack"` rules; the Asana page only `connector=="asana"`.

### 4.4 New `config_store` functions

Mirror the existing `put_/get_/list_/delete_` style, each paged like `list_repos`/`list_capabilities` and each validating ids:
`list_slack_workspaces` / `get_slack_workspace` / `put_slack_workspace` / `set_slack_workspace_status` / `delete_slack_workspace`; `list_channels(team_id)` / `put_channel_policy` / `delete_channel_policy`; `list_trigger_rules(connector=None)` / `get_trigger_rule` / `put_trigger_rule` / `delete_trigger_rule`; `list_channel_requests(status=None)` / `get_channel_request` / `put_channel_request` / `resolve_channel_request` / `delete_channel_request`.

### 4.5 Channel onboarding requests (user-initiated, admin-approved)

A user in a Slack channel runs **`/sdlc-onboard-channel [agent …]`** to request that *their* channel be onboarded for specific agents. The request is captured `pending` (`channel_request` record, §4.3-adjacent) — it grants nothing on its own; **approval by an admin is the only path to access** (a user cannot self-serve). Flow:

1. **Request** — the Slack receiver (`slack_webhook._record_channel_request`) writes a `channel_request` row via the dispatch-side `trigger_grants.put_channel_request` (validates the Slack ids + agent ids). The requester is the immutable `slack:<team>:<user>` (T-4, auditable). The receiver replies ephemerally: *"📨 Request filed… an admin will review it."*
2. **Review** — the request appears in the Slack connector page's **Requests** tab (`GET /admin/channel-requests?status=pending`).
3. **Approve** (`POST /admin/channel-requests/{id}/approve`) — `admin._decide_channel_request` composes the effect explicitly: (a) `put_channel_policy(mode="allow")` for the channel (the WHERE axis), (b) a **permit** `trigger_rule` per approved agent, keyed on the **channel group** `channel:<team>:<channel>` (so anyone triggering *from that channel* is permitted — the receiver stamps that group into `principal_groups`), with a deterministic `rule_id` per (team, channel, agent) so a replay overwrites rather than duplicates, and (c) `resolve_channel_request(status="approved")`. The agent scope must be **concrete**: an admin override via `approved_agents` wins, else the request's `requested_agents`; an empty or `"*"` scope is rejected (400) — approval is never a silent no-op nor a workspace-wide over-grant. Only a **pending** request can be decided (409 otherwise), so a double-approve can't create duplicate grants and an approve-then-deny can't leave grants behind.
4. **Deny** (`POST /admin/channel-requests/{id}/deny`) — records the decision; grants nothing.

Record shape: `pk="chan_req#<uuid>"` → `{kind:"channel_request", team_id, channel_id, channel_name, requested_by, requested_agents[], status:"pending"|"approved"|"denied", created_at, decided_by, decided_at}`.

---

## 5. Cedar-backed trigger authorization

### 5.1 The `TriggerPolicyStore` (AVP)

A **new** AVP policy store, `TriggerPolicyStore`, namespace `SdlcTrigger`, `ValidationSettings.Mode: STRICT`. Because it is the *only* trigger-authz mechanism (no allowlist fallback), it is **required foundation infrastructure** — provisioned alongside the dashboard's existing `DashboardPolicyStore`, not gated behind a toggle. (`DeploySlack` still gates the Slack-specific receiver + secrets; that's a separate axis. An unset `TRIGGER_POLICY_STORE_ID` is a deployment error and, being fail-closed, denies every trigger.) Schema:

The design is **data-driven**: the store holds a small **fixed** policy set (§5.2); the WHO grants + WHERE channel posture are DATA (DynamoDB rows) the router reads and passes to `IsAuthorized` as **entity attributes** — so granting a user is a DynamoDB write, never a `CreatePolicy`, and the policy count stays constant regardless of user/rule count. A policy-per-user is the AVP anti-pattern this avoids. Schema:

```jsonc
{
  "SdlcTrigger": {
    "entityTypes": {
      "Group":  { "shape": {"type":"Record","attributes":{}} },
      "User":   { "memberOfTypes": ["Group"],
                  "shape": {"type":"Record","attributes":{
                    "groups": {"type":"Set","element":{"type":"String"}},
                    "email":  {"type":"String","required":false} }} },
      "Agent":  { "shape": {"type":"Record","attributes":{
                    // the agent's resolved grant sets (from trigger_rule rows)
                    "allowedPrincipals": {"type":"Set","element":{"type":"String"}},
                    "deniedPrincipals":  {"type":"Set","element":{"type":"String"}},
                    "allowedGroups":     {"type":"Set","element":{"type":"String"}},
                    "deniedGroups":      {"type":"Set","element":{"type":"String"}} }} }
    },
    "actions": {
      "Trigger": {
        "appliesTo": {
          "principalTypes": ["User"],
          "resourceTypes": ["Agent"],
          "context": { "type":"Record", "attributes": {
            "workspace":      {"type":"String"},
            "channel":        {"type":"String"},
            "source":         {"type":"String"},
            "channelAllowed": {"type":"Boolean"}   // WHERE posture, resolved in Python
          }}
        }
      }
    }
  }
}
```

### 5.2 The fixed policy set (authored once, in the CFN template)

Three static policies, authored once, that **never change** as users/rules are added (`trigger_authz.FIXED_POLICIES` is the source of truth + template contents):

```cedar
// P1 permit — principal is granted directly, or via a granted group
permit(principal, action == SdlcTrigger::Action::"Trigger", resource)
when {
  resource.allowedPrincipals.contains(principal) ||
  principal.groups.containsAny(resource.allowedGroups)
};

// P2 forbid (wins) — principal or one of its groups is explicitly denied
forbid(principal, action == SdlcTrigger::Action::"Trigger", resource)
when {
  resource.deniedPrincipals.contains(principal) ||
  principal.groups.containsAny(resource.deniedGroups)
};

// P3 forbid (wins) — the channel isn't allowed for the workspace
forbid(principal, action == SdlcTrigger::Action::"Trigger", resource)
when { !context.channelAllowed };
```

- **Default-deny**: no allowed membership ⇒ no permit ⇒ deny. An admin opts subjects in by writing `trigger_rule` permit rows (which become `allowedPrincipals`/`allowedGroups` data).
- **Two axes compose**: WHO (P1/P2, from `trigger_rule` rows) and WHERE (P3, from the `slack_channel` allow/deny rows resolved to one `channelAllowed` boolean). Cedar **forbid-wins** gives an explicit deny — or a blocked channel — precedence over any permit.
- The channel-posture math (allowlist vs denylist + the allow/deny rows) is resolved in **Python** (`trigger_grants.channel_allowed`) rather than Cedar, so the posture logic is unit-testable and Cedar stays a single boolean check.

### 5.3 Router evaluation — `infra/dispatch/trigger_authz.py` + `trigger_grants.py`

`trigger_grants.py` (a dispatch-side reader mirroring `fleet_config.py`, short-TTL cached) resolves the DATA per request: `agent_grants(agent, workspace)` → the four grant sets from `trigger_rule` rows; `channel_allowed(workspace, channel)` → the WHERE boolean from workspace policy + `slack_channel` rows. `trigger_authz.is_authorized` assembles those into the `Agent` resource attributes + the principal's `groups` + `context.channelAllowed`, then calls AVP against the fixed policy set:

```python
def is_authorized(*, principal, agent_id, source, context) -> Decision:
    store = os.environ.get("TRIGGER_POLICY_STORE_ID")
    if not store:
        return Decision(allow=False, reason="authz-store-unconfigured")  # fail closed
    grants = trigger_grants.agent_grants(agent_id, context.get("workspace", ""))      # DDB
    channel_ok = trigger_grants.channel_allowed(context.get("workspace",""),          # DDB
                                                context.get("channel_id",""))
    resp = avp.is_authorized(
        policyStoreId=store,
        principal={"entityType":"SdlcTrigger::User","entityId":principal},
        action={"actionType":"SdlcTrigger::Action","actionId":"Trigger"},
        resource={"entityType":"SdlcTrigger::Agent","entityId":agent_id},
        context={"contextMap": {
            "workspace":{"string":context.get("workspace","")},
            "channel":{"string":context.get("channel_id","")},
            "source":{"string":source},
            "channelAllowed":{"boolean": channel_ok}}},
        entities={"entityList":[
            {"identifier":{...User...}, "parents":[Group... per group],
             "attributes":{"groups": set(groups), "email": ...}},
            {"identifier":{...Agent...}, "attributes":{
                "allowedPrincipals": set(grants.allowed_principals),
                "deniedPrincipals":  set(grants.denied_principals),
                "allowedGroups":     set(grants.allowed_groups),
                "deniedGroups":      set(grants.denied_groups)}}]},
    )
    return Decision(allow=(resp["decision"]=="ALLOW"),
                    reason=_reason_from(resp.get("determiningPolicies")))
```

A grant-read failure fails closed (`authz-unavailable`) before AVP is called.

Fails closed on any exception, a non-ALLOW decision, OR an unconfigured store —
identical discipline to `auth._authorize`. `Decision.allow` is a plain bool (there
is no tri-state / fallback signal).

### 5.4 Router seam — `infra/dispatch/router.py`

`authorize_trigger` (returns `(allowed, reason)`; `check_authorization` is a thin
bool wrapper over it) is the single authorization path:

```python
def authorize_trigger(agent_config, sender, source, source_context=None):
    agent_id = agent_config.get("agent_id", "?")
    if not sender or sender in _UNRESOLVED_SENDERS:      # T-4
        return False, "unresolved-sender"
    decision = trigger_authz.is_authorized(
        principal=sender, agent_id=agent_id,
        source=source, context=source_context or {})
    return decision.allow, ("" if decision.allow else decision.reason)
```

- **One mechanism, all sources.** There is no per-capability allowlist and no
  back-compat branch. GitHub, Asana, and Slack all authorize here. The principal
  is **namespaced by source** (`github:<login>` / `asana:<gid>` /
  `slack:<team>:<uid>`) so ids from different sources can't collide. The
  receivers emit source-native ids (GitHub login, Asana gid) and Slack already
  namespaces; the router applies the `github:`/`asana:` prefix centrally via
  `namespaced_principal(sender, source)` — one place, so a hand-authored rule and
  the live principal always use the same form. GitHub/Asana carry no
  workspace/channel context (those Cedar clauses no-op for them).
- The unresolved-sender sentinel is rejected **before** any AVP call, so it is
  never passed as a principal (T-4).
- On deny, the handler records `blocked_authz`, emits `TriggerDenied`, and posts
  the reason to the origin thread (§3.2).
- Router IAM gains `verifiedpermissions:IsAuthorized` on the trigger store; env
  gains `TRIGGER_POLICY_STORE_ID` (required).

### 5.5 Rule management (`infra/dashboard`)

Because grants are **data**, rule management is just DynamoDB writes — there is **no** per-rule AVP sync, no `CreatePolicy`/`DeletePolicy`, and therefore no persist↔project divergence to guard against:
- **create/edit rule** → `config_store.put_trigger_rule` (a single `PutItem`).
- **delete rule** → `config_store.delete_trigger_rule` (a single `DeleteItem`).
- **channel policy** → `put_channel_policy` / `delete_channel_policy`.

The router picks the change up on its next grant-cache refresh (short TTL, `trigger_grants.reset_cache` in tests) — the same propagation model as the repo allowlist (`fleet_config`). This is strictly simpler than the earlier per-rule-policy design *and* avoids the AVP policy-count anti-pattern. (`trigger_policy_sync.py` from the first cut was deleted.)

### 5.6 Principal identity & groups

- **Principal:** `slack:<team_id>:<user_id>` — workspace-scoped and immutable (Slack user ids aren't self-editable; parallels the Asana `.gid` rationale, threat T-4). Never the display name.
- **Email attribute:** best-effort `users.info` resolution, passed as the Cedar `User.email` attribute so admins may *also* write email-based rules.
- **Groups (`memberOfTypes`):** source of membership is **dashboard-maintained role mappings** (admin maps a Slack user/usergroup → a fleet role) — simplest, no extra Slack scopes, admin-controlled. Slack **usergroups** (`usergroups.users.list`) are a documented fast-follow. The receiver/enrichment resolves the principal's groups and passes them as `principal_groups` in context.
- **Cross-source unification (core, not optional):** GitHub (`github:<login>`) and Asana (`asana:<gid>`) authorize through the same `Trigger` action from day one; their `context.channel`/`workspace` are absent so channel/workspace clauses no-op. The flat `authorization.users` list is **removed** (config_store no longer stores it and `render_registry` no longer emits an `authorization` block). v2 is unreleased and nothing is deployed, so there is no legacy allowlist data to carry over — admins author `trigger_rule` grants directly in the dashboard; anything ungranted is default-deny.

---

## 6. Slack receiver (`infra/dispatch/slack_webhook.py`, new)

Mirrors `asana_webhook.py` / `github_webhook.py` as a thin source adapter.

### 6.1 Correctness requirements (the parts a naive port gets wrong)

- **3-second ack.** Verify → dedup → async `Event`-invoke the router → return 200 immediately. The user-visible "on it" ack is posted by the **router** (§3.1 step 12), never on the receiver's request path — so a slow `chat.postMessage` can't blow the 3 s budget.
- **Signature scheme.** Slack signs a constructed basestring, not the raw body:
  `expected = "v0=" + hmac_sha256(signing_secret, f"v0:{timestamp}:{raw_body}")`, compared timing-safe; reject if `|now - X-Slack-Request-Timestamp| > 300`. `verify_slack_signature(signing_secret, timestamp, raw_body, provided, *, max_skew=300)` lives in `mentions.py`. Base64-decode the API-Gateway body **before** building the basestring.
- **App-level signing secret, verify FIRST.** The signing secret is **per-app** (one per Slack app — only *bot tokens* are per-workspace), stored at `/sdlc-agents/<stage>/slack/signing-secret`. Crucially, the `url_verification` handshake payload carries **no `team_id`**, so verification must not depend on one: the receiver verifies the signature (app-level) and answers the challenge *before* resolving the workspace. An unconfigured signing secret → 503. After verification, real events/commands resolve `team_id` and require the workspace to be onboarded + enabled (`is_workspace_enabled`); unknown/disabled → no-op (events) / ephemeral notice (commands). Multi-workspace lives in the **bot token** (per-workspace, for replies) + the workspace/channel/grant rows, not the signing secret.
- **Route by path, not content.** Classify events vs slash commands by the API-Gateway resource path only — never by sniffing the body (a JSON `app_mention` whose text contains `command=` must not be mis-parsed as a form command).
- **Retry & dedup.** Slack retries with `X-Slack-Retry-Num` and re-sends the same `event_id`. Dedup on a TTL'd `slack-event#<id>` item, but **record it only AFTER the event processed cleanly** (`_already_seen` before, `_mark_seen` after): a delivery that fails mid-dispatch returns 500 and is *not* marked, so Slack's retry is processed rather than swallowed by its own marker. A read/write error on the dedup store fails open (a duplicate dispatch is tolerated by the router's assignment-id + concurrency guard; a dropped mention is not).
- **Bot-loop prevention.** Ignore events with `bot_id`, `subtype=="bot_message"`, or authored by our own bot user — else the agent's reply re-triggers it.
- **Two inbound shapes.** `event_callback` (JSON: `app_mention`, `message`) incl. the `url_verification` `challenge` handshake (parity with Asana's `X-Hook-Secret`); and **slash commands** (`application/x-www-form-urlencoded`: `command`, `text`, `channel_id`, `user_id`, `trigger_id`, `response_url`) — agent id from the command name (`/workitems` → validated against the registry), text as instruction.
- **Mention format.** `app_mention` text arrives as `<@U0FLEETBOT> @workitems …`; strip the leading bot mention (`re.sub(r'^\s*<@\w+>\s*', '', text)`) then `mentions.resolve_mention` — identical registry-driven resolution to GitHub/Asana. Supports a single fleet Slack app (`@fleetbot @agent …`).
- **Threading.** Capture `thread_ts` (fall back to `ts`) so the ack and the final result land in-thread.
- **Context populated for Cedar.** `context = {workspace: team_id, channel_id, thread_ts, message_ts, requester_email?, principal_groups?}`; `sender = "slack:<team_id>:<user_id>"`. The router **denies** if `channel_id` is missing when the workspace is allowlist-mode (fail-closed on missing context).

### 6.2 Single fleet Slack app (recommended)

One Slack app (`@fleetbot`), one manifest, resolution by registry mention — far less operational overhead than per-agent apps, and registry-driven resolution already supports it. Scopes: `app_mentions:read`, `chat:write`, `commands`, `users:read.email`; events: `app_mention`; slash commands per agent (or one `/fleet` with the agent as the first token).

---

## 7. Outbound replies

- **`reply.post_slack_message(team_id, channel, text, thread_ts=None) -> bool`** (`infra/dispatch/reply.py`): fetch that workspace's bot token from SSM per-invocation (never a module global — T-8), `chat.postMessage`, return bool (non-fatal on failure; emit a metric — matching the existing `post_github_comment`/`post_asana_comment` contract).
- **`router._post_block_reply`** gains a `slack` branch; the router also posts the success ack (`slack` only, so GitHub/Asana behavior is unchanged).
- **Agent result round-trip** (`agents/*/tools/post_results.py`): make `post_results` slack-aware. **Recommended:** route the agent's final result through a **gateway Slack MCP target** to preserve the gateway-only policy/observability chokepoint (`agents/shared/tools/gateway.py`); the minimal alternative is the router posting the completion summary from `complete_assignment`. Direct `chat.postMessage` in the tool is the least preferred (bypasses the gateway).

---

## 8. Enrichment (`infra/dispatch/enrichment.py`)

Add `elif source == "slack":` branches to `derive_trace_refs` (emit `slack_workspace`, `slack_channel`, `slack_thread_ts`; scan the instruction for a Jira key) and `derive_participants` (requester = Slack user / resolved email). The dashboard then renders Slack runs with real trace links and participants, at parity with GitHub/Asana.

---

## 9. Connectors UI — inside the Admin panel

The dashboard SPA (`dashboard/`, dependency-free React + hash routing) gains a **Connectors** area **within Admin**. Decisions locked: **Connectors lives inside the Admin panel**; **access rules are per connector**.

### 9.1 Routing (`dashboard/src/App.tsx`)

Admin is already one top-level view (`#/admin`). Extend the hash scheme *under* it — no new dependency:
- `#/admin` → Admin landing (existing fleet-config + capabilities), now with a **Connectors** entry.
- `#/admin/connectors` → Connectors index (card grid).
- `#/admin/connectors/<id>` → a connector sub-page (`slack` | `asana` | `github`), validated against the connector registry; unknown → index.

`viewToHash`/`hashToView` learn these; the existing GitHub-App manifest-callback normalization is **retargeted** to `#/admin/connectors/github` (the callback-exchange effect moves with `GitHubAppPanel` — §9.4).

### 9.2 Connector registry (`dashboard/src/connectors/registry.ts`)

Descriptor-driven, mirroring the fleet's registry-driven ethos (adding a connector = one module, not edits scattered across the app):

```ts
export interface ConnectorDescriptor {
  id: "slack" | "asana" | "github";
  label: string; blurb: string; icon: React.ReactNode;
  requiredRole: "admin";
  useStatus: (api: DashboardApi) => ConnectorStatus;   // drives the card health badge
  Page: React.ComponentType<ConnectorPageProps>;
}
export const CONNECTORS: ConnectorDescriptor[] = [slackConnector, asanaConnector, githubConnector];
```

Both the index and the router iterate `CONNECTORS`.

### 9.3 Shared chrome (`dashboard/src/connectors/ConnectorLayout.tsx`)

Every sub-page gets identical chrome: a connection-status header (from `useStatus`) and a standard **tab strip**:
- **Connection** — connect/setup flow + secret/token status (never secret values).
- **Triggers** — which events fire agents (maps to capability `triggers{source:[...]}`).
- **Access rules** — the **per-connector** Cedar trigger rules (§4.3, filtered to this connector) + the **Test access** simulator.
- **Activity** — recent dispatches from this source + receiver error / `TriggerDenied` metrics.

Reuses existing primitives: `usePolling`/`useApi` (`hooks.ts`), `StatusPill` (`components.tsx`), the modal + `run()` write-wrapper pattern from `AdminView`, and `styles.css` classes.

### 9.4 Sub-pages (`dashboard/src/connectors/`)

- **`SlackConnectorPage.tsx`** — *Connection:* onboard **≥1 workspace** (manifest download + install, per-workspace token/secret status, enable/disable/remove). *Access rules:* two composing axes — WHO trigger rules (subject → agent → workspace → permit/forbid) and WHERE per-workspace channel allow/deny — plus the simulator. *Activity:* Slack-sourced runs, `slack-webhook` errors, `TriggerDenied`.
- **`AsanaConnectorPage.tsx`** — first real UI for what `scripts/bootstrap_asana_webhook.py` does by hand: PAT + webhook-secret status, handshake/registration state, bot-user GIDs + Agent-field enum mapping (currently env-only). Access-rules tab shows Asana trigger rules (channel clauses absent).
- **`GitHubConnectorPage.tsx`** — absorbs `GitHubAppPanel` verbatim (registration/install status + the manifest-callback exchange effect currently in `AdminView`). Cross-links to **Admin → Fleet config** for repo onboarding (repos stay there — they're a GitHub *resource/authz* concern, not the connection).

`AdminView.tsx` shrinks: it keeps repos + capabilities + settings, drops the inline `GitHubAppPanel`, and gains a Connectors card/link.

### 9.5 Access-rules UX & the simulator

The Access-rules tab is the "comprehensive admin capability to ensure the right users get access or a proper reject." It surfaces both axes — the WHO rule builder (subject user/group → agent → workspace → permit/forbid) and the WHERE per-workspace channel allow/deny — and a **Test access** panel wired to `POST /admin/trigger-rules/simulate` that runs a read-only AVP `IsAuthorized` for a hypothetical (subject, agent, workspace, channel) and shows **ALLOW/DENY + the deciding policy** — so an admin can answer "why was this rejected?" before a user ever hits it.

### 9.6 API client & types (`dashboard/src/api.ts`, `types.ts`)

Add methods mirroring `listRepos`/`onboardRepo`:
`listSlackWorkspaces` / `onboardSlackWorkspace` / `deleteSlackWorkspace` / `slackManifest(teamName?)`, `listChannels(teamId)` / `putChannelPolicy` / `deleteChannelPolicy`, `listTriggerRules(connector)` / `createTriggerRule` / `deleteTriggerRule` / `simulateAccess(input)`, and `connectorStatus(id)` for the badges. Add `SlackWorkspace`, `ChannelPolicy`, `TriggerRule`, `ConnectorStatus` to `types.ts`. The `DashboardApi` request core is unchanged.

---

## 10. Admin API routes (`infra/dashboard/admin.py`)

All `auth.is_admin`, fail-closed, using the existing `_route` + `_sync_after_write` pattern.

| Method + path | Purpose |
|---|---|
| `GET/POST /admin/slack/workspaces`, `DELETE …/{team_id}` | Onboard / list / remove workspaces |
| `GET /admin/slack/workspaces/{team_id}/manifest` | Slack app manifest (parallels `github-app/setup/manifest`) |
| `GET/POST /admin/slack/channels`, `DELETE …/{team_id}/{channel_id}` | Per-workspace channel allow/deny |
| `GET/POST /admin/trigger-rules?connector=<id>`, `DELETE …/{rule_id}` | Per-connector rule CRUD (pure `config_store` writes; no AVP projection — §5.5) |
| `POST /admin/trigger-rules/simulate` | Read-only AVP `IsAuthorized` dry-run → ALLOW/DENY + deciding policy |

Admin Lambda IAM gains `verifiedpermissions:CreatePolicy/DeletePolicy/ListPolicies/GetPolicy/IsAuthorized` on the trigger store, and SSM read/write for the per-workspace Slack secret paths (`/sdlc-agents/${Stage}/slack/*`).

---

## 11. Infrastructure (`infra/foundation/template.yaml`)

- **`TriggerPolicyStore`** (`AWS::VerifiedPermissions::PolicyStore`, STRICT schema §5.1) — **always provisioned** (required foundation, alongside `DashboardPolicyStore`); NOT toggle-gated. Optional policy templates (`AWS::VerifiedPermissions::PolicyTemplate`) are a convenience for hand-authored rules; the admin path renders static policies (§5.5).
- **`SlackWebhookFunction`** (mirrors `AsanaWebhookFunction`) + `WebhookApi` routes `POST /slack/events` and `POST /slack/commands`; env `REGISTRY_PARAM`, `DISPATCH_FUNCTION`, `FLEET_CONFIG_TABLE` (workspace lookup); SSM read on `/sdlc-agents/${Stage}/slack/*`; `lambda:InvokeFunction` on the router. Gated by `DeploySlack` (default `false`).
- A dedup table (or a reused TTL'd item shape) for `slack_event#<id>`, with the receiver's conditional-write perms.
- **Router**: `verifiedpermissions:IsAuthorized` on the trigger store + `TRIGGER_POLICY_STORE_ID` env.
- CloudWatch error alarm for `slack-webhook-${Stage}` (mirror `asana-webhook-errors-${Stage}`).
- **Outputs**: `TriggerPolicyStoreId`, `SlackEventsEndpoint`, `SlackCommandsEndpoint`.
- Reuses the existing per-policy enforcement rollout idea: consider a `TriggerAuthzEnforcement` env (LOG_ONLY vs enforce) so authz can roll out log-only first, like the gateway policy.

---

## 12. Delivery phases

Slack-specific pieces are gated by `DeploySlack`; the Cedar trigger-authz store is core (always on).

1. **Trigger-authz spine — DONE (data-driven).** `trigger_authz.py` (fixed policy set + entity assembly), `trigger_grants.py` (dispatch-side DDB reader for the WHO grants + WHERE channel posture), the single `authorize_trigger` router path (no allowlist, no back-compat), config-store workspace/channel/rule records. The flat `authorization.users` source was removed from config_store/admin/registry. Grants are DynamoDB data — no per-rule AVP policy. 346 tests green.
2. **Foundation infra — DONE.** `TriggerPolicyStore` (always-on) + the 3 fixed policies in the stack; router `TRIGGER_POLICY_STORE_ID`/IAM. `sam validate` clean. (Nothing is deployed, so there is no legacy `authorization.users` data to migrate — admins author grants directly.)
3. **Slack receiver + multi-workspace onboarding — DONE** (behind `DeploySlack`): `slack_webhook.py`, `mentions.verify_slack_signature`, `reply.post_slack_message`, enrichment slack branch, admin routes, `scripts/bootstrap_slack.py`. **Plus the channel-onboarding request flow (§4.5).**
4. **Connectors UI inside Admin — DONE** (`dashboard/src/connectors/`): registry + routing + `ConnectorLayout`, GitHub page (relocated `GitHubAppPanel`), Asana page, Slack page (workspaces / channels / access rules / requests queue / simulator). `npm run build` clean.
5. **Wire it live in dev.** Deploy with `DeploySlack=true` in a dev stage; onboard a test workspace, author rules, exercise the simulator, verify allow + every reject reason in-thread. *(Deploy-time step — not a code change.)*
6. **Docs/skills**, threat-model rows, then enable in gamma/prod.

---

## 13. Testing

Mirrors the existing receiver/authz suites. The old allowlist tests were rewritten onto the Cedar model (they no longer exist as allowlist assertions); the guardrail/repo-binding tests stub `authorize_trigger` since they aren't about authz. Suite is 346 green after the spine + collapse + data-driven rework.

- **`trigger_authz`**: ALLOW / default-deny / fail-closed on AVP error / grant-read error / **fail-closed when store unset**; and the `IsAuthorized` call shape — Agent resource carries the grant sets, principal carries its groups+email, context carries workspace/channel/source/channelAllowed.
- **`config_store`**: new record CRUD + id validation (reject Cedar-metachar / bad team/channel/user ids).
- **`admin`**: workspace + channel + rule CRUD (pure DDB writes, no AVP projection); simulate endpoint; `is_admin` gating; connector routes.
- **`trigger_grants`**: WHO resolution (permit/forbid, user/group, agent+workspace wildcard matching); WHERE resolution (allowlist/denylist posture, unknown-workspace fail-closed, non-Slack allowed); cache TTL refresh.
- **`slack_webhook`**: valid signature → correct dispatch payload; bad signature → 401; missing/disabled workspace → no dispatch; **replay** rejected; `url_verification` echoes challenge; slash command (form-encoded) parsed; `app_mention` bot-prefix stripping → correct resolution; unknown mention → 200 no-op; **bot-loop** ignored; **dedup** (same `event_id` twice → one dispatch); base64 body decoded before signature; per-workspace secret selection.
- **`reply`**: `post_slack_message` success/failure returns bool; per-workspace token fetch.
- **`mentions.verify_slack_signature`**: timing-safe, skew window, prefix.
- **`enrichment`**: slack trace_refs + participants.
- **Router**: authz-deny posts a Slack notice + records `blocked_authz` + emits `TriggerDenied`.
- **SPA**: hash round-trips for `#/admin/connectors/<id>`; registry-driven index (every descriptor id unique + routable); Vite/TS `npm run build` stays green; `BASE_URL` subpath handling holds for the new routes.

---

## 14. Threat model additions (`docs/threat-model.md`)

New components **C-18 Slack receiver + Slack app**, **C-19 `TriggerPolicyStore` (AVP)**. New threats, each mapped to a mitigation above:

| ID | Threat | Mitigation |
|----|--------|-----------|
| T-32 | Slack signature bypass | `v0` HMAC over the basestring; fail-closed on missing secret (§6.1) |
| T-33 | Replay of a captured delivery | 5-minute timestamp window + `event_id` dedup (§6.1) |
| T-34 | Bot-loop / self-trigger | ignore `bot_id` / bot subtypes / own bot user (§6.1) |
| T-35 | Sender spoofing | authorize on immutable `slack:<team>:<uid>`, never display name (§5.6, cf. T-4) |
| T-36 | Bot-token / signing-secret exposure | per-workspace SSM SecureString, fetched per-invocation, never a module global (§4.1, §7; cf. T-8) |
| T-37 | Cross-workspace confusion | Cedar `context.workspace` pin + receiver selects secret by `team_id`; unknown/disabled workspace rejected (§5.2, §6.1) |
| T-38 | Authz bypass via missing channel context | receiver always populates `channel_id`; router denies when absent under allowlist posture (§6.1) |
| T-39 | Rule/AVP divergence (UI shows a rule Cedar doesn't enforce, or vice-versa) | persist↔project rollback invariant, direction-correct on create vs delete (§5.5) |

Plus a `1.10` changelog row and DF entries for the Slack inbound + reply flows and the trigger-authz decision.

---

## 15. Open decision (resolved by §17)

**Group-membership source for group-scoped rules (§5.6):** ~~dashboard-maintained role mappings vs. Slack usergroups vs. corporate-SSO groups.~~ **Resolved.** Group membership is carried on the **identity record** (§16) and assigned during user onboarding (§17). It is source-agnostic by construction — a user's groups apply to every source (GitHub / Asana / Slack) because authz keys on the resolved identity, not the source handle. Slack usergroups / SSO-group *import* remains a possible enrichment source that could populate identity `groups`, but is not required. This retires the "dashboard role-mapping vs. Slack usergroup" fork.

---

# Part II — Identity, Permission Groups, and Notifications (v3, PROPOSED)

> **Status: PROPOSED (not built).** Part I above (Slack source + trigger authz + Connectors UI) is shipped. Part II specifies the next increment: a **cross-source identity map** keyed on email, **permission groups** as the recommended access mechanism, and **self-serve interactive Slack notifications**. It also removes the `DeploySlack` deploy gate in favor of admin workspace-onboarding. These sections are dependency-ordered: identity (§16) underpins groups (§17), which underpin notification mentions (§18). Nothing here is implemented; sections are independently shippable in the order given.

## 16. Cross-source identity map

### 16.1 Why

Today a person is four disjoint handles with no join: `slack:<team>:<uid>`, `github:<login>`, `asana:<gid>`, and the dashboard Cognito `sub`. `trigger_authz` already reserves an optional `requester_email` Cedar attribute (`infra/dispatch/trigger_authz.py`) but **nothing populates it** — so traceability fractures at every source boundary, and a notification can't reliably reach "the right person" across platforms. The identity map makes **email the golden id for joining** the handles, so every assignment record, authz decision, and notification mention resolves to one person regardless of which source it came from.

### 16.2 Record shape — uuid-keyed, email is a join attribute (not the key)

Email is the golden *join* id but **cannot be the primary key**: a first-touch from GitHub or Asana often yields no email (GitHub exposes it only if public; Asana needs the added scope). So the record is keyed on a synthetic `identity_id` (uuid) and email is an attribute + a lookup index, populated the moment any source reveals it.

```
pk = identity#<uuid>            kind = "identity"
  identity_id:  <uuid>
  email:        jane@corp.com   # golden join id; may be "" until a source reveals it
  display_name: "Jane Doe"
  status:       pending | active | disabled
  handles:      { github: "jane-gh",
                  asana:  "12009...",           # gid
                  slack:  { "T04": "U123", "T09": "U777" } }   # per-workspace
  groups:       [ "<group_id>", ... ]           # §17; source-agnostic membership
  verified:     { github: true, asana: false, slack: true }   # per-handle trust
  created_from: { source: "github", handle: "jane-gh", at: <ts> }
  onboarded_by: "<admin cognito sub>" | "self-first-touch"
  merged_from:  [ "<identity_id>", ... ]        # audit trail of merges (§16.5)
```

**Global secondary indexes** (resolution is O(1) from any direction):
- `email → identity_id`
- one handle GSI per source: `gsi_handle` on a synthesized `handle_key` attribute list — `github:jane-gh`, `asana:12009...`, `slack:T04:U123` — so a source resolver looks up by the exact namespaced handle it holds.

### 16.3 The resolver — `identity.py` (shared by dispatch + dashboard)

One function every source calls on the way in:

```
resolve_identity(source, handle, known={email?, display_name?}, source_context={}) -> Identity
```

Behavior:
1. **Look up** the namespaced handle via `gsi_handle`.
2. **Hit** → return it, and **backfill** any new identifiers in `known` that the record lacks (progressive enrichment — a GitHub-born record gains `slack` + `email` the first time the person speaks in Slack). Backfill of a *handle for a different source* is additive; backfill of an email that already keys a **different** record triggers a merge (§16.5).
3. **Miss** → **get-or-create**: write a new `identity#<uuid>` with whatever `known` carries, `status = pending`, `created_from` stamped, and file a **user-onboarding request** (§16.4). Return the pending record.

The resolver never assumes a dashboard/Cognito user exists — non-admins who never touch the dashboard still get a record on first touch from any source.

### 16.4 First-touch onboarding gate (default-deny, admin-approved)

A `pending` identity is **known but not usable**: the Router rejects its dispatch (fail-closed, mirroring channel onboarding). On the rejecting reply, the copy branches on what we can promise:

- **Org-owned GitHub repo** (owner type = Organization on the resolved installation): the App can read the org member list (`GET /orgs/{org}/members`) and resolve the member's email, so we *can* email them on completion. Reply (verbatim):
  > A request to onboard you has been sent to the SDLC admin and you'll receive an email when onboarding is complete. If you have any questions, please contact your SDLC Admin. Once onboarded, please try your request again.
- **Personal GitHub repo** (owner type = User; no org directory, likely no email): reply without an email promise:
  > You're not onboarded to the SDLC fleet. Please contact your SDLC Admin for onboarding.
- **Slack / Asana** (email available from `users.info` / the added Asana scope): use the org-style copy; completion notice goes to the originating thread and to the resolved email.

**Reply-every-time, request-once.** Every mention from a pending/unknown user gets the reply (the user needs the feedback loop; it's a threaded reply, not a ping-storm). But only **one** `user_req#<uuid>` row is filed per identity — a subsequent mention updates/no-ops the request rather than stacking duplicates in the admin queue. Clean split: **request deduped, reply always.**

```
pk = user_req#<uuid>            kind = "user_request"
  identity_id:   <uuid>         # the pending identity created at first touch
  source:        github | asana | slack
  source_context: {...}         # repo+issue/PR, or team+channel+thread_ts — for the completion reply
  proposed_email: "..." | ""    # resolved from org members / users.info when available
  status:        pending | approved | denied
  created_at / decided_by / decided_at
```

**Admins can also create identities proactively** in the dashboard (email + handles up front, born `active`) — the lazy path is the fallback, not the only path.

### 16.5 Merges

Lazy creation from email-less sources means one person can spawn two records before we know they're the same (GitHub-first record with no email; later a Slack-first record with email). Reconciliation:

- **Auto-merge on email match (high confidence):** when a resolve/backfill surfaces an email that already keys an `active` record, fold the two into one `identity_id`, union the handles, keep `merged_from` for audit. Email is the golden id, so an email collision is the strongest signal.
- **Admin-reviewed merge (weaker signals):** a handle-only or display-name collision is surfaced in the dashboard for an admin to confirm (approve-as-new vs. link-into-existing) — this is also the natural moment at user-onboarding approval to show "likely matches."

### 16.6 Verification & authz trust

A cross-source handle link is **authz-load-bearing only when verified**. Because **admin approval is the trust event** (a human confirms "this Slack user is `github:jane-gh`"), handles attached during admin onboarding/merge are `verified: true` by construction — no separate self-verify challenge needed. Unverified links (e.g. an auto-backfilled handle we haven't confirmed) are fine for *display* and *best-effort* notification routing, but must not be trusted to grant access or to author an authoritative audit claim.

### 16.7 Traceability payoff

Every assignment record and every `trigger_authz` decision is stamped with the resolved `email` (populating the long-reserved `requester_email` Cedar attribute), not just the source handle. Authz grants and group membership can then be authored against **email/identity** and apply across all sources at once — one grant, every platform. This is the audit spine the rest of Part II builds on.

## 17. Permission groups

### 17.1 Why groups (and why this is a seam, not a bolt-on)

`trigger_authz`'s fixed Cedar policy set **already** evaluates `principal.groups.containsAny(resource.allowedGroups)` and the denied-group mirror — group membership is first-class in the policy today; there has simply been no place to *define* groups or *assign* users. `trigger_grants` already distinguishes `RULE_SUBJECT_GROUP` from `RULE_SUBJECT_USER`. So permission groups fill a designed-for seam: **no Cedar policy change**, groups are more grant *data*.

### 17.2 Model

- **`perm_group#<id>`** — a named group ("edtech-engineers", "adr-reviewers"). Metadata only (name, description, `recommended` flag).
- **Membership on the identity record** — `identity#<uuid>.groups: [<group_id>, ...]`. Adding a user to a group during onboarding (§16.4 approval) is an edit to *their* identity, so the group's access applies to **every** source that identity resolves from.
- **Group access = group-scoped `trigger_rule` rows** — a group is a subject: author `trigger_rule` with `subject_type: group`, `subject_id: <group_id>` (the schema already supports this). "Create a group and give it access" = create the `perm_group#` row + author group-scoped trigger rules. **One grant mechanism, not two.** Direct per-user `trigger_rule`s remain the scalpel for exceptions; **groups are the recommended default.**

```
pk = perm_group#<id>            kind = "perm_group"
  group_id:    <id>
  name:        "edtech-engineers"
  description: "..."
  recommended: true
  created_by / created_at
```

### 17.3 Dispatch path

At dispatch, `resolve_identity` (§16) yields the user's `groups`; the Router passes them to `trigger_authz.is_authorized` as the principal's `groups` attribute + `Group` parent entities (the code already assembles these — it just receives `[]` today). The existing fixed policies do the rest. **Scope:** flat/global groups first (matches how `trigger_rule` workspace-wildcards work today); workspace/org-scoped groups only if a real need appears.

### 17.4 Onboarding becomes: approve + assign groups

The user-onboarding approval (§16.4) *is* the permission/access step: admin approves the pending identity → assigns one or more groups → status `active`. This is decision **A** from the design discussion (identity + baseline access are one coherent gate for a newcomer), with groups as the mechanism — avoiding the confusing "onboarded but can't do anything" state.

## 18. Self-serve interactive Slack notifications

### 18.1 What's posted today vs. proposed

**Today the fleet posts to Slack exactly once path:** a block/reject notice via `reply.post_slack_message` (single caller, `router._post_block_reply`). There is **no** success reply, no result-posted-back, no notifications. This section adds a configurable, tiered notification capability. It is **self-serve** (a channel configures its own subscription — a subscription only *receives*, it grants no access, so no admin approval is needed), unlike channel onboarding (§4.5) which gates *access* and does require admin approval.

### 18.2 New inbound route — `/slack/interactions`

Block Kit actions (checkboxes, dropdowns, buttons) and modal submits (`view_submission`) post back on a **separate** endpoint from the Events API, so the Slack receiver gains a third route `/slack/interactions` (same `v0` signature verification + replay window as the others). Flow:

1. User runs `/sdlc-notify` in a channel → receiver calls `views.open` with the `trigger_id` → renders a **modal**.
2. Modal fields (Block Kit):
   - **Three tier checkbox groups**, each expandable into specific event types:
     - **Actionable** — an agent posted something a human should engage with: a decomposition/proposal awaiting approval, a PR that needs review, a question back to the requester. *(These mention people — §18.4.)*
     - **Informative** — no action needed: a run kicked off, a run completed cleanly, an agent picked up a task. *(No mention.)*
     - **Error** — something errored and may have stopped a flow/run: run failed, guardrail tripped, credential expired, assignment stuck.
   - **Repo multi-select dropdown**, **bounded to repos the channel is actually granted** (a channel can't subscribe to notifications for a repo it has no co-repo/trigger access to — enforced against the channel's grants, not a free list).
   - **Severity floor** (e.g. "error + actionable only, skip informative").
3. `view_submission` → writes a `notif_sub#` row. Editing re-opens the modal pre-filled from the row.

```
pk = notif_sub#<team>#<channel>   kind = "notif_sub"
  team_id / channel_id
  repos:      [ "owner/repo", ... ]   # validated ⊆ channel's granted repos
  tiers:      { actionable: [event...], informative: [event...], error: [event...] }
  min_severity
  created_by  # slack:<team>:<uid>, resolved to identity for audit
  created_at / updated_at
```

### 18.3 Notification sources (fan-out)

Two origins, deliberately separated:
- **Fleet-internal events** — run started / completed / failed, guardrail tripped, awaiting-approval. The Router and agents already have these signals; fan-out matches them against `notif_sub#` rows and posts to subscribed channels.
- **External SCM events** — PR opened/merged, issue activity. The **GitHub App webhook already receives these deliveries** (it ignores non-mentions today), so routing matching events to subscribed channels reuses the same signed webhook — low marginal cost.

Delivery reuses `reply.post_slack_message` with the per-workspace bot token.

### 18.4 Threading & mentions — anti-spam + connect-the-right-people

- **One root message per unit of work** (per `assignment_id`, or per PR number), with all follow-ups posted **into that thread** via `thread_ts`. A whole run's lifecycle (`started → proposal ready → completed`) collapses into one thread, not N channel posts.
- **Mentions only on Actionable + Error tiers.** Informative stays unmentioned so it pings no one.
- **Mention the resolved person, in the right workspace** — via the identity map (§16), "the PR author" / "the requester" → `<@U…>` using that person's `slack` handle **for this team**. This is the concrete payoff of email-as-golden-id: an event whose actor is a GitHub login gets routed to the correct Slack user. If identity can't resolve (unverified / no slack handle for the team), **degrade to an unmentioned post** rather than mis-ping.

### 18.5 Dashboard parity

The Connectors → Slack sub-page gains a read/edit view of channel subscriptions (admins can see/adjust what a channel self-configured), consistent with how the panel already surfaces channels and trigger rules.

## 19. Retire the `DeploySlack` deploy gate — DONE

`DeploySlack` gated only inert-at-rest, serverless resources: the `SlackWebhookFunction` Lambda ($0 idle), its CloudWatch error alarm, and two stack **outputs**. Meanwhile a **runtime gate already exists** — the receiver rejects any delivery whose `team_id` isn't an onboarded, enabled, active `slack_workspace` row (`trigger_grants.is_workspace_enabled`), and `POST /admin/slack/workspaces` is how an admin onboards one. So the deploy flag was **redundant with the admin-onboarding gate**. The `SlackEnabled` condition + `DeploySlack` parameter are removed; the Lambda + its three routes + endpoint outputs always deploy.

**Change:** remove the `SlackEnabled` CloudFormation condition so the Slack Lambda + its three routes (`/slack/events`, `/slack/commands`, `/slack/interactions`) and the endpoint outputs **always deploy**. Slack goes "live" only when an admin onboards a workspace. The always-on public endpoint is safe because it **fails closed**: no signing secret → 503; no onboarded workspace → dropped. This yields one onboarding story — deploy the (serverless, inert) infra once; enable via Admin — matching the fleet's "simple deploy, configure in Admin" posture.

## 20. New surfaces & threat-model deltas (Part II)

**New/changed surfaces:** `identity#` rows + `email`/`gsi_handle` GSIs + `identity.py` resolver (dispatch + dashboard); `user_req#` rows + admin approval queue; `perm_group#` rows + group membership on identity + group-scoped `trigger_rule`s; `/slack/interactions` route + modal builders; `notif_sub#` rows + fan-out from fleet + SCM events; Slack scope `users:read.email`, added Asana user-email scope, GitHub org-member read; retire `SlackEnabled` condition.

**Threat-model additions (to draft in `docs/threat-model.md`):**
- **Identity-link spoofing** — a wrongly-claimed cross-source handle is impersonation. Mitigated: links are authz-load-bearing only when `verified`, and verification = admin approval (§16.6).
- **Merge poisoning** — a bad auto-merge fuses two people. Mitigated: auto-merge only on exact email match (strongest signal); weaker signals go to admin review (§16.5); `merged_from` audit trail.
- **Notification-scope leak** — a channel subscribing to a repo it shouldn't see. Mitigated: subscription repos validated ⊆ the channel's granted repos (§18.2).
- **New public inbound route** (`/slack/interactions`) — same `v0` signature + replay-window verification as the existing routes; fails closed.
- **Group over-grant** — a group's `trigger_rule`s apply to every member across every source. Accepted/By-design; bounded by admin authoring groups (`recommended`) and per-user forbid rules as the scalpel.

Plus a `2.1` changelog row (identity map + permission groups + interactive notifications + `DeploySlack` retirement) and DF entries for identity resolution, the interactions route, and notification fan-out.

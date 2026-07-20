# Connectors: Multi-Workspace Slack + Cedar-Backed Trigger Authorization
## Admin-managed event sources with per-connector access rules

> **Status: target design — not yet built.** This spec defines (1) a first-class **Slack** dispatch source at parity with GitHub and Asana, (2) a **Cedar-backed trigger-authorization** layer (a third AVP policy store, evaluated by the Dispatch Router) that decides *who* may trigger *which* agent *where*, and (3) a **Connectors** section **inside the Admin panel** of the dashboard SPA, with a dedicated sub-page per connector (Slack, Asana, GitHub) that owns that connector's connection, triggers, and **per-connector access rules**. It supersedes the Slack sections of `dispatch-agent-assignment-spec.md` §4c and closes roadmap items around Slack dispatch and connector UX. Nothing here ships until the phases in §12 land; the router, receivers, config store, and SPA described as "today" are the current code under `infra/dispatch/`, `infra/dashboard/`, and `dashboard/`.

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
- Migrating GitHub/Asana authorization onto Cedar in this spec (it becomes *possible* — see §5.6 — but is scheduled as an optional Phase, not required).
- Automating Slack-side setup (creating usergroups, channels). The admin does Slack-side configuration; the fleet consumes it.

---

## 2. Where this fits in the existing architecture

Three authorization decision points exist or are introduced. Two already exist; this spec adds the third.

| # | Surface | Decides | Mechanism (today / proposed) |
|---|---------|---------|------------------------------|
| A | **Dashboard API** (`infra/dashboard/auth.py`) | May this operator read / this admin write the dashboard? | AVP `DashboardPolicyStore`, static Cedar, Cognito groups as parent entities. **Unchanged.** |
| B | **Tool calls** (`infra/dashboard/fleet_policy.py` + `policy_sync.py`) | May this agent call this tool on this repo? | AgentCore Gateway Cedar engine, policies **dynamically synced** from admin config. **Unchanged.** |
| C | **Trigger** (`infra/dispatch/router.py::check_authorization`) | May this sender trigger this agent from this source / workspace / channel? | **TODAY:** flat in-code allowlist match on `capability.authorization.users`, no notion of workspace/channel. **PROPOSED:** AVP `TriggerPolicyStore`, Cedar, rules dynamically synced from admin config. |

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

The unit an admin creates on a connector's **Access rules** tab.

```jsonc
{
  kind: "trigger_rule",
  rule_id: "<uuid>",
  connector: "slack" | "asana" | "github",   // which sub-page owns it (per-connector rules)
  subject_type: "user" | "group",
  subject_id: "slack:T0ACME:U123" | "group:eng-oncall",
  agent_id: "workitems" | "*",                // "*" = any agent
  workspace: "T0ACME" | "*",                  // slack only; "*" = any (or absent for non-slack)
  channels: ["C0ENG","C0REL"] | ["*"],        // slack only; ["*"] = any channel
  effect: "permit" | "forbid",
  avp_policy_id: "<id>",                       // set after AVP projection (§5.5)
  created_by, created_at
}
```

`connector` scopes the rule to exactly one sub-page — satisfying **per-connector rules**. The Slack page lists only `connector=="slack"` rules; the Asana page only `connector=="asana"`.

### 4.4 New `config_store` functions

Mirror the existing `put_/get_/list_/delete_` style, each paged like `list_repos`/`list_capabilities` and each validating ids:
`list_slack_workspaces` / `get_slack_workspace` / `put_slack_workspace` / `set_slack_workspace_status` / `delete_slack_workspace`; `list_channels(team_id)` / `put_channel_policy` / `delete_channel_policy`; `list_trigger_rules(connector=None)` / `get_trigger_rule` / `put_trigger_rule` / `set_trigger_rule_policy_id` / `delete_trigger_rule`.

---

## 5. Cedar-backed trigger authorization

### 5.1 The `TriggerPolicyStore` (AVP)

A **new** AVP policy store, `TriggerPolicyStore`, namespace `SdlcTrigger`, `ValidationSettings.Mode: STRICT`, gated by a new `EnableTriggerAuthz` CloudFormation condition (default off). Schema:

```jsonc
{
  "SdlcTrigger": {
    "entityTypes": {
      "Group":  { "shape": {"type":"Record","attributes":{}} },
      "User":   { "memberOfTypes": ["Group"],
                  "shape": {"type":"Record","attributes":{ "email": {"type":"String","required":false} }} },
      "Agent":  { "shape": {"type":"Record","attributes":{}} }
    },
    "actions": {
      "Trigger": {
        "appliesTo": {
          "principalTypes": ["User"],
          "resourceTypes": ["Agent"],
          "context": { "type":"Record", "attributes": {
            "workspace": {"type":"String"},
            "channel":   {"type":"String"},
            "source":    {"type":"String"}
          }}
        }
      }
    }
  }
}
```

### 5.2 Policy templates (authored once, in the CFN template)

Fine-grained rules are unbounded and admin-authored, so they map to AVP **policy templates** + **template-linked policies** — the AVP-native "grant this principal this action on this resource under condition Z" mechanism. Templates (`AWS::VerifiedPermissions::PolicyTemplate`) are static scaffold; the admin API instantiates one linked policy per rule row.

- **`TplPermitInChannels`** (per-user, allowlist channel posture):
  ```cedar
  permit(principal == ?principal, action == SdlcTrigger::Action::"Trigger", resource == ?resource)
  when {
    context.workspace == "<WS>" &&
    (["*"] == ["<CH...>"] || context.channel in [<CH...>])
  };
  ```
  Rendered per rule with the workspace literal and channel-id list (or an unconditional channel clause when `channels == ["*"]`).
- **`TplPermitGroupInChannels`** — same, but `principal in ?principal` (a `Group` entity), for `subject_type=="group"`.
- **`TplForbidChannels`** — `forbid(...) when { context.workspace == "<WS>" && context.channel in [<CH...>] };` — backs `deny`-mode channels and per-user forbids. Cedar's **forbid-wins** makes a deny beat any permit.
- The store has **no blanket permit**: default-deny is the posture. An admin opts subjects in with permit rules.

Denylist channel posture is expressed as a workspace-level permit (`context.workspace == "<WS>"`, any channel) plus `TplForbidChannels` rows for the denied channels.

### 5.3 Router evaluation — `infra/dispatch/trigger_authz.py` (new)

```python
def is_authorized(*, principal: str, agent_id: str, source: str, context: dict) -> Decision:
    store = os.environ.get("TRIGGER_POLICY_STORE_ID")
    if not store:
        return Decision(allow=None)          # signal: caller uses legacy allowlist
    groups = context.get("principal_groups", [])   # from receiver/enrichment
    resp = avp.is_authorized(
        policyStoreId=store,
        principal={"entityType":"SdlcTrigger::User","entityId":principal},
        action={"actionType":"SdlcTrigger::Action","actionId":"Trigger"},
        resource={"entityType":"SdlcTrigger::Agent","entityId":agent_id},
        context={"contextMap": {
            "workspace":{"string":context.get("workspace","")},
            "channel":  {"string":context.get("channel_id","")},
            "source":   {"string":source},
        }},
        entities={"entityList":[{
            "identifier":{"entityType":"SdlcTrigger::User","entityId":principal},
            "parents":[{"entityType":"SdlcTrigger::Group","entityId":g} for g in groups],
            "attributes": ({"email":{"string":context["requester_email"]}}
                           if context.get("requester_email") else {}),
        }]},
    )
    return Decision(allow=(resp["decision"]=="ALLOW"),
                    reason=_reason_from(resp.get("determiningPolicies")))
```

Fails closed on any exception or non-ALLOW — identical discipline to `auth._authorize`.

### 5.4 Router seam — `infra/dispatch/router.py`

`check_authorization` gains `source_context` and becomes:

```python
def check_authorization(agent_config, sender, source, source_context):
    if not sender or sender in _UNRESOLVED_SENDERS:      # T-4, unchanged
        return AuthzResult(False, "unresolved-sender")
    decision = trigger_authz.is_authorized(
        principal=sender, agent_id=agent_config["agent_id"],
        source=source, context=source_context)
    if decision.allow is None:                            # AVP not deployed / off
        return _legacy_allowlist(agent_config, sender)    # today's behavior, unchanged
    return AuthzResult(decision.allow, decision.reason)
```

- **Back-compat is the default:** with `TRIGGER_POLICY_STORE_ID` unset, behavior is byte-for-byte today's allowlist. GitHub/Asana and all existing tests are unaffected until the toggle is on.
- On deny, the handler records `blocked_authz`, emits `TriggerDenied`, and posts the reason to the origin thread (§3.2).
- Router IAM gains `verifiedpermissions:IsAuthorized` on the trigger store; env gains `TRIGGER_POLICY_STORE_ID`.

### 5.5 Rule management + the divergence invariant (`infra/dashboard`)

A new `trigger_policy_sync.py` (parallel to `policy_sync.py`) projects a rule row into AVP:
- **create rule** → `config_store.put_trigger_rule` (persist), then AVP `CreatePolicy` (template-linked, with the rule's principal/resource + rendered template values), then `set_trigger_rule_policy_id`.
- **delete rule** → AVP `DeletePolicy(avp_policy_id)`, then `delete_trigger_rule`.
- **On AVP failure, roll back the row** (or leave `pending` and report), so **enforcement and config never diverge** — the same invariant `admin.py` upholds for repo→gateway sync ("an admin action never widens dispatch while the policy lags"). The direction matters: on *create*, persist-then-project and roll back the row if projection fails (never leave a rule the UI shows but Cedar doesn't enforce); on *delete*, revoke-in-AVP-first (never leave Cedar enforcing a rule the UI thinks is gone).
- No-op with a logged warning when `TRIGGER_POLICY_STORE_ID` is unset (store not provisioned) — the admin can still author rows; they project when the store lands.

### 5.6 Principal identity & groups

- **Principal:** `slack:<team_id>:<user_id>` — workspace-scoped and immutable (Slack user ids aren't self-editable; parallels the Asana `.gid` rationale, threat T-4). Never the display name.
- **Email attribute:** best-effort `users.info` resolution, passed as the Cedar `User.email` attribute so admins may *also* write email-based rules.
- **Groups (`memberOfTypes`):** source of membership is **dashboard-maintained role mappings** (admin maps a Slack user/usergroup → a fleet role) — simplest, no extra Slack scopes, admin-controlled. Slack **usergroups** (`usergroups.users.list`) are a documented fast-follow. The receiver/enrichment resolves the principal's groups and passes them as `principal_groups` in context.
- **Cross-source unification (optional Phase, §12):** GitHub (`github:<login>`) and Asana (`asana:<gid>`) can adopt the same `Trigger` action later; their `context.channel`/`workspace` are absent so channel/workspace clauses no-op. This would retire the flat `authorization.users` list. Out of scope to *require* here.

---

## 6. Slack receiver (`infra/dispatch/slack_webhook.py`, new)

Mirrors `asana_webhook.py` / `github_webhook.py` as a thin source adapter.

### 6.1 Correctness requirements (the parts a naive port gets wrong)

- **3-second ack.** Verify → dedup → async `Event`-invoke the router → return 200 immediately. The user-visible "on it" ack is posted by the **router** (§3.1 step 12), never on the receiver's request path — so a slow `chat.postMessage` can't blow the 3 s budget.
- **Signature scheme.** Slack signs a constructed basestring, not the raw body:
  `expected = "v0=" + hmac_sha256(signing_secret, f"v0:{timestamp}:{raw_body}")`, compared timing-safe; reject if `|now - X-Slack-Request-Timestamp| > 300`. Add `verify_slack_signature(signing_secret, timestamp, raw_body, provided, *, max_skew=300)` to `mentions.py` so all signing logic stays in one hardened module. Base64-decode the API-Gateway body **before** building the basestring (parity with the fix already in both existing receivers).
- **Multi-workspace secret selection.** Parse `team_id` from the (pre-verification) envelope, look up the workspace row, and verify against **that workspace's** signing secret. Unknown/disabled workspace → 200 no-op (events) / 401 — never dispatch. This is the crux of multi-workspace support.
- **Retry & dedup.** Slack retries with `X-Slack-Retry-Num` and re-sends the same `event_id`. Dedup on `event_id` via a conditional `PutItem` on a TTL'd `slack_event#<id>` item; a duplicate acks 200 and no-ops.
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

- **`SlackConnectorPage.tsx`** — *Connection:* onboard **≥1 workspace** (manifest download + install, per-workspace token/secret status, enable/disable/remove). *Access rules:* per-workspace channel allow/deny + trigger rules (subject → agent → workspace → channels → permit/forbid) + the simulator. *Activity:* Slack-sourced runs, `slack-webhook` errors, `TriggerDenied`.
- **`AsanaConnectorPage.tsx`** — first real UI for what `scripts/bootstrap_asana_webhook.py` does by hand: PAT + webhook-secret status, handshake/registration state, bot-user GIDs + Agent-field enum mapping (currently env-only). Access-rules tab shows Asana trigger rules (channel clauses absent).
- **`GitHubConnectorPage.tsx`** — absorbs `GitHubAppPanel` verbatim (registration/install status + the manifest-callback exchange effect currently in `AdminView`). Cross-links to **Admin → Fleet config** for repo onboarding (repos stay there — they're a GitHub *resource/authz* concern, not the connection).

`AdminView.tsx` shrinks: it keeps repos + capabilities + settings, drops the inline `GitHubAppPanel`, and gains a Connectors card/link.

### 9.5 Access-rules UX & the simulator

The Access-rules tab is the "comprehensive admin capability to ensure the right users get access or a proper reject." It lists this connector's rules, offers a builder (subject user/group → agent → workspace → channels → permit/forbid), and a **Test access** panel wired to `POST /admin/trigger-rules/simulate` that runs a read-only AVP `IsAuthorized` for a hypothetical (subject, agent, workspace, channel) and shows **ALLOW/DENY + the deciding policy** — so an admin can answer "why was this rejected?" before a user ever hits it.

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
| `GET/POST /admin/trigger-rules?connector=<id>`, `DELETE …/{rule_id}` | Per-connector rule CRUD (→ AVP via `trigger_policy_sync`, §5.5) |
| `POST /admin/trigger-rules/simulate` | Read-only AVP `IsAuthorized` dry-run → ALLOW/DENY + deciding policy |

Admin Lambda IAM gains `verifiedpermissions:CreatePolicy/DeletePolicy/ListPolicies/GetPolicy/IsAuthorized` on the trigger store, and SSM read/write for the per-workspace Slack secret paths (`/sdlc-agents/${Stage}/slack/*`).

---

## 11. Infrastructure (`infra/foundation/template.yaml`)

- **`TriggerPolicyStore`** (`AWS::VerifiedPermissions::PolicyStore`, STRICT schema §5.1) + policy templates (`AWS::VerifiedPermissions::PolicyTemplate`, §5.2), gated by `EnableTriggerAuthz` (default `false`).
- **`SlackWebhookFunction`** (mirrors `AsanaWebhookFunction`) + `WebhookApi` routes `POST /slack/events` and `POST /slack/commands`; env `REGISTRY_PARAM`, `DISPATCH_FUNCTION`, `FLEET_CONFIG_TABLE` (workspace lookup); SSM read on `/sdlc-agents/${Stage}/slack/*`; `lambda:InvokeFunction` on the router. Gated by `DeploySlack` (default `false`).
- A dedup table (or a reused TTL'd item shape) for `slack_event#<id>`, with the receiver's conditional-write perms.
- **Router**: `verifiedpermissions:IsAuthorized` on the trigger store + `TRIGGER_POLICY_STORE_ID` env.
- CloudWatch error alarm for `slack-webhook-${Stage}` (mirror `asana-webhook-errors-${Stage}`).
- **Outputs**: `TriggerPolicyStoreId`, `SlackEventsEndpoint`, `SlackCommandsEndpoint`.
- Reuses the existing per-policy enforcement rollout idea: consider a `TriggerAuthzEnforcement` env (LOG_ONLY vs enforce) so authz can roll out log-only first, like the gateway policy.

---

## 12. Delivery phases

Each phase is independently shippable and toggle-gated; nothing is user-visible until its toggle flips.

1. **Trigger-authz spine (dark).** `SdlcTrigger` AVP store + templates (`EnableTriggerAuthz=false`), `trigger_authz.py`, router seam with back-compat fallback, `trigger_policy_sync.py`, config-store rule/workspace/channel records. Unit-tested; zero runtime behavior change (store off).
2. **Slack receiver + multi-workspace onboarding** behind `DeploySlack=false`: `slack_webhook.py`, `verify_slack_signature`, `reply.post_slack_message`, enrichment slack branch, admin routes + config-store workspace/channel/secret handling, `bootstrap_slack.py`.
3. **Connectors UI inside Admin** (`dashboard/`): registry + routing + `ConnectorLayout`, GitHub page (relocate `GitHubAppPanel`), Asana page (surface existing state), Slack page. Ship the GitHub relocation first (pure refactor, no backend dep).
4. **Wire it live in dev.** Deploy with `DeploySlack=true` + `EnableTriggerAuthz=true` in a dev stage; onboard a test workspace, author rules, exercise the simulator, verify allow + every reject reason in-thread.
5. **(Optional) Unify GitHub/Asana authz on Cedar** (§5.6) — retire the flat `authorization.users` list.
6. **Docs/skills**, threat-model rows, then enable in gamma/prod via the toggles.

---

## 13. Testing

Mirrors the existing receiver/authz suites; keep the 316 green (back-compat path guarantees GitHub/Asana are untouched until the toggle is on).

- **`trigger_authz`**: ALLOW / deny / forbid-wins / fail-closed on AVP error / back-compat when store unset / group membership / channel+workspace context / missing-channel denial.
- **`config_store`**: new record CRUD + id validation (reject Cedar-metachar / bad team/channel/user ids).
- **`admin`**: workspace + channel + rule CRUD; rule→AVP projection and **rollback on AVP failure**; simulate endpoint; `is_admin` gating; connector routes.
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

## 15. Open decision (single remaining)

**Group-membership source for group-scoped rules (§5.6):** dashboard-maintained role mappings (recommended — no extra Slack scopes, admin-controlled) vs. Slack usergroups (`usergroups.users.list`, needs `usergroups:read`) vs. corporate-SSO groups. This determines the `User → Group` parent entities the router passes to Cedar. Recommendation stands: ship dashboard-maintained mappings first, add Slack usergroups as a fast-follow. Everything else in this spec is resolved.

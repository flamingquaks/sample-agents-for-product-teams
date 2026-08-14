# PDLC Agent Fleet

Autonomous AI agents for the software development lifecycle, deployed on Amazon
Bedrock AgentCore. This file is the canonical project reference — start here.

## What it is

A multi-agent fleet. Users trigger an agent by `@mention` from GitHub, Asana,
Slack, Jira, or Confluence; a **Dispatch Router** Lambda resolves the mention,
authorizes it, applies a prompt-injection guardrail, and invokes the agent's
**AgentCore Runtime** container. Agents act back on GitHub/Asana/Jira/Confluence
through an **AgentCore Gateway** (Cedar-enforced), never directly.

Agents (each self-contained under `agents/<name>/`):

| Agent | Role | Aliases |
|-------|------|---------|
| `workitems` | PO/PM — decomposition, status, risk, sync | `@pm` `@status` `@plan` |
| `researcher` | BA — research, competitive intel, backlog | `@ba` `@research` `@analyze` |
| `docwriter` | Tech writer — API docs, guides, release notes | `@docs` `@doc` `@writer` |
| `adr` | ADR linker — tags issues, reviews PRs vs the ADR library | `@decisions` `@architecture` |
| `reviewer` | Code reviewer — inline PR findings (correctness, safety, soundness) | `@review` `@cr` |

## Project structure

```
agents/            Agent code (Strands SDK, containerized → AgentCore Runtime)
  <name>/          agent.py · prompts.py · tools/ · project_config.py · Dockerfile · requirements.txt · tests/
  shared/          Shared helpers (bedrock.py model builder, gateway client, assignment, tools)
infra/
  dispatch/        Dispatch Router + Asana & GitHub webhook receivers, SCM broker/interceptor, guardrail (Lambda)
  dashboard/       Fleet monitoring + admin SPA backend (query + admin Lambdas), capability build/deploy, AVP authz
  foundation/      SAM/CloudFormation — all shared AWS resources
cedar/             Cedar policies for agent TOOL access (advisory source; enforced copy in dashboard/fleet_policy.py)
dashboard/         Admin + monitoring SPA (React + Vite); Admin view onboards agents + repos
docs/              Design docs, specs, threat model, roadmap
.github/workflows/ Lint + security scans only (no deploy/dispatch — those are server-side now)
```

## Tech stack

- **Language / framework**: Python 3.12 · Strands Agents SDK · Amazon Bedrock AgentCore Runtime (containerized).
- **Models — Bedrock Mantle**: agents call the OpenAI-compatible **bedrock-mantle** endpoint (`agents/shared/bedrock.py` → Strands `OpenAIModel`), NOT `bedrock-runtime`. Default `anthropic.claude-sonnet-5` (env `BEDROCK_MODEL_ID`; never send `temperature` — Sonnet 5 rejects it). Auth is a short-term bearer token minted from the runtime role (`aws-bedrock-token-generator`), no stored secret. **Fleet-wide cost attribution** via a single shared Mantle **project** — provisioned in the stack as `AWS::BedrockMantle::Project` (`DeployMantleProject`, on by default; the resource type must be `activate-type`'d in the account+region first), or a pre-existing id via the `MantleProjectId` param — whose id flows to `MANTLE_PROJECT_ID` runtime env → the `OpenAI-Project` header; one project fleet-wide because a dispatch may span repos (co-repo modes), so per-repo attribution is meaningless. ADR Titan embeddings and the Router's `apply_guardrail` stay on classic `bedrock-runtime`.
- **Guardrail (fail-closed, T-1/2/3)**: the prompt-injection guardrail is attached to every model call via Mantle headers (`X-Amzn-Bedrock-Guardrail*`); `build_model` raises if `BEDROCK_GUARDRAIL_ID` is unset. The Router also runs an edge `apply_guardrail` check before dispatch.
- **Tool access — Gateway-only**: every agent routes ALL MCP tool calls through the **AgentCore Gateway** (`GATEWAY_MCP_URL` required; agent refuses to start without it), one SigV4 client via the runtime role. Gateway fronts Asana (direct MCP) and GitHub (via the SCM broker Lambda), enforces a Cedar policy engine, runs the SCM co-repo interceptor. No direct-to-vendor path.
- **Auth (external)**: Asana via OAuth/PAT in SSM. **GitHub via per-owner GitHub App installation tokens** minted server-side (broker/interceptor/webhook Lambdas); agents hold no GitHub credential. App private key in Secrets Manager, id/slug/webhook-secret in SSM.
- **API authz — Amazon Verified Permissions (Cedar)**: the dashboard API's authorization (`infra/dashboard/auth.py`) is decided by **AVP** evaluating Cedar policies — `Read` (operators+admins) and `Write` (admins). Distinct from the agent-tool Cedar at the Gateway. Add a permission = add a Cedar policy, no code change.
- **Multi-repo**: multi-owner; each repo declares which OTHER repos a dispatch from it may act on (`co_repo_mode`: isolated | group | all); per-agent product access enforced at the interceptor + credential layers.
- **Infra**: AWS SAM. **Deploy**: base platform via `scripts/deploy_fleet.py` (foundation stack + agent build source + dashboard SPA); `scripts/bootstrap.py` is the thin one-time privileged setup. There is no GitHub-OIDC / CI deploy path — it was retired.

## How things flow

- **Auto-review (reviewer agent)** (`docs/specs/reviewer-agent-spec.md`): the `reviewer` agent reads a PR diff and posts one COMMENT review with inline correctness/safety/soundness findings (each with a concrete failure scenario) + an advisory commit status — never approve/merge/push (same COMMENT-only + `contents:read` enforcement as `adr`, plus a `statuses:write` tier clamped to success/pending). `@reviewer` works via `mentions.py`; auto-review on `pull_request.opened`/`synchronize` rides the **automation engine extended to GitHub** (`automation.github_facts` + `github_webhook._run_automation`, bot-actor guarded), enabled per-repo in the GitHub connector's **Auto-review** tab. Incrementality + don't-re-raise key on a per-PR memory ledger (last-reviewed head SHA + rebase-stable finding fingerprints).
- **Triggers → dispatch**: GitHub App webhook (`infra/dispatch/github_webhook.py`), Asana webhook (`asana_webhook.py`), Slack webhook (`slack_webhook.py`, always deployed; Slack goes live only when an admin onboards a workspace), and Jira/Confluence webhooks (`jira_webhook.py`/`confluence_webhook.py`, always deployed, inert until an Atlassian site is onboarded — events arrive via the `atlassian-events` **Forge forwarder** (`forge/atlassian-events/`), authenticated per delivery by a Forge Invocation Token verified RS256 against Atlassian's JWKS; **no webhook secret exists**) verify their deliveries and async-invoke the Dispatch Router. No per-repo GitHub Actions workflow. All share `infra/dispatch/mentions.py` for verification + registry-backed @mention resolution (agent ids/aliases from the live registry, short-TTL cached), so a UI-onboarded agent is mentionable from every source with no per-receiver code change — a new trigger source is a thin adapter over this module. (Asana's assignment + custom-field paths stay built-in-only.)
- **Atlassian connector** (`docs/specs/atlassian-connector-spec.md` — Jira + Confluence on one foundation): one `atlassian_site#` row per site (one service account + scoped API token in SSM, per-product enable), connected one-click in Connectors → Atlassian. Agents get `JiraTarget`/`ConfluenceTarget` gateway tools via Lambda brokers (`jira_broker.py`/`confluence_broker.py`) — curated no-delete schemas, container allowlists (Confluence **reads** are scoped too: onboarding a space = fleet-wide read), per-space `write_mode: propose|direct` + `write_agents`, `base_version` optimistic concurrency, broker-owned Markdown↔storage conversion. The `automation.py` engine runs data-driven event→agent rules (`automation_rule#` rows; synthetic `automation:<connector>:<rule_id>` principals with auto-managed grants; bot-actor/cooldown/hourly-ceiling/chain-depth brakes). Trace refs `jira_key`/`confluence_page` are native; `/sdlc-notify me` adds per-user Slack DMs (`notif_pref#`).
- **Trigger authorization (the sole mechanism)**: the Router authorizes every dispatch via AVP against the `TriggerPolicyStore` (`infra/dispatch/trigger_authz.py`), a small FIXED Cedar policy set evaluating grant sets read as data from `trigger_rule` + `slack_channel` rows (`trigger_grants.py`). Granting a user is a DynamoDB write, not a new policy (avoids the AVP policy-per-user anti-pattern). Fail-closed. There is no per-capability `authorization.users` allowlist (removed). Slack users can request channel access via `/sdlc-onboard-channel`; admins approve/deny in the dashboard Connectors → Slack panel.
- **Identity, groups & first-touch onboarding** (`infra/dispatch/identity.py`, spec §16–§17): every dispatch resolves the sender to a cross-source **identity** (`identity#` row; email is the golden join id; get-or-create + enrich on first touch from any source). A first touch creates a **`pending`** identity + a `user_req#` request and replies telling the user to get onboarded (org-repo copy promises an email, personal-repo copy points at the admin) — reply-every-time, request-once. An admin approves in the dashboard **Connectors → Access** panel, which flips the identity `active`, assigns **permission groups** (`perm_group#`, the recommended access mechanism — group access is group-scoped `trigger_rule` rows, membership is on the identity), and verifies its handles. The resolved email + groups flow into trigger authz, so a grant applies across GitHub/Asana/Slack at once. The Slack receiver seeds a **verified** `display_name` + `email` from `users.info` (the workspace's authenticated directory — `reply.slack_user_profile`) into the dispatch context, so the identity stores a real name and joins on email (T-42: only an authenticated directory may seed the golden email; unverified fields are dropped). The admin API decorates trigger-rule / channel-request / notif-sub rows with friendly **labels** (`_LabelDirectory`: person = display_name→email→handle; `channel:T:C`→`#name`; team→workspace name; group id→group name) so the dashboard never shows raw Slack ids.
- **Durable repo work + pause/resume** (`docs/specs/durable-repo-work-and-resume-spec.md`): conversation durability = Strands `S3SessionManager` keyed by `assignment_id` (`agents/shared/durable.py`, `SessionBucket`); workspace durability = GitHub `wip/<assignment_id>` branches via the git workspace tools (`agents/shared/tools/workspace.py` — clone/run/commit+push, credentials minted per git op by `infra/dispatch/workspace_token_vendor.py`, never at rest in the container). An agent blocked on a human calls `ask_user` → Strands interrupt → pause protocol (push every repo clean + verify, record `workspace_snapshot`+`interrupt_id`, flip `awaiting_input`; fail loud if the push can't land). Resume = in-thread `@sdlc-agents` reply: the Slack webhook resolves the `thread_binding#` row, the router guardrails the reply, takes the `awaiting_input→resuming` conditional lock, and re-invokes the runtime with the saved interrupt id + snapshot. A reply on a `completed` thread starts a NEW assignment linked via `parent_assignment_id`. Token usage ACCUMULATES (DynamoDB ADD) across segments — never overwrite.
- **Notifications** (`infra/dispatch/notify.py` + `slack_notify.py`, spec §18): channels self-serve tiered Slack notifications (`actionable`/`informative`/`error`) via the `/sdlc-notify` Block Kit modal (submitted on the `/slack/interactions` route → `notif_sub#` row). The Router fans fleet lifecycle events out to subscribed channels, threaded per unit-of-work, with @mentions (resolved to the right Slack user via the identity map) only on actionable/error tiers. Repo scope is bounded to onboarded repos.
- **Onboarding an agent (capability)**: admin clicks Onboard in the dashboard → a `capability#<id>` row is written → the shared `sdlc-agent-builder-<stage>` CodeBuild project builds `agents/<name>` → a build-completion event invokes the `capability-deployer` Lambda, which creates the per-agent runtime IAM role (under IAM path `/sdlc-agents/capabilities/*`, capped by a permissions boundary) + the AgentCore runtime, waits READY, marks the capability `active`.
- **Registry**: the Dispatch Router registry is **rendered from the active capability rows** (`config_store.render_registry`) and written to SSM (`/sdlc-agents/${Stage}/registry`) on every change — replacing the old `.dispatch/agents.yaml` + `sync_registry.py`. Routability keys on "enabled + has a live runtime", so a rebuild never drops a working agent.
- **Weekly security rebuild**: a scheduled Lambda rebuilds every active agent's container so images pick up patches; a failed rebuild leaves the running agent up.
- **Onboarding a repo**: admin onboards `owner/repo` → GitHub App install is verified → the row drives the dispatch allowlist + Cedar repo policy. (Model cost attribution is fleet-wide, not per-repo — see the shared Mantle project above.)

## Conventions

- Agents NEVER close issues, merge PRs, or delete tasks (Cedar-enforced). Work decomposition uses propose → human-approve → execute.
- Custom tools are structured task prompts, not business logic — they return instructions the LLM orchestrates. Built-in agents' system prompts live in `prompts.py` beside the agent code; dashboard-authored custom agents carry theirs in the capability row's `system_prompt` field (read by the generic base agent, `agents/_base/` — see `docs/specs/agent-authoring-spec.md`).
- The privileged deploy actions (IAM role + runtime + build) live only on event-triggered Lambdas (`capability-deployer`), never on the internet-facing admin API — which holds only `codebuild:StartBuild`.

## Future (low-effort pivots kept open)

- **AWS Agent Registry** as the org-wide agent catalog: the `render_registry`/`publish_registry` seam is isolated so the DynamoDB-rendered registry can additionally sync to Agent Registry (Preview; no CFN yet) without disturbing dispatch.

## Working notes

- Build & verify: dashboard/dispatch/shared Python suites run under `pytest` (moto for AWS); `dashboard/` SPA builds with `npm run build`; validate infra with `sam validate` in `infra/foundation/`.
- `docs/` has the design docs, per-agent specs, and the living **threat model** (`docs/threat-model.md`). `docs/aws-deploy.md` is the full deploy surface.

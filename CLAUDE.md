# PDLC Agent Fleet

Autonomous AI agents for the software development lifecycle, deployed on Amazon
Bedrock AgentCore. This file is the canonical project reference — start here.

## What it is

A multi-agent fleet. Users trigger an agent by `@mention` from GitHub or Asana;
a **Dispatch Router** Lambda resolves the mention, authorizes it, applies a
prompt-injection guardrail, and invokes the agent's **AgentCore Runtime**
container. Agents act back on GitHub/Asana through an **AgentCore Gateway**
(Cedar-enforced), never directly.

Agents (each self-contained under `agents/<name>/`):

| Agent | Role | Aliases |
|-------|------|---------|
| `workitems` | PO/PM — decomposition, status, risk, sync | `@pm` `@status` `@plan` |
| `researcher` | BA — research, competitive intel, backlog | `@ba` `@research` `@analyze` |
| `docwriter` | Tech writer — API docs, guides, release notes | `@docs` `@doc` `@writer` |
| `adr` | ADR linker — tags issues, reviews PRs vs the ADR library | `@decisions` `@architecture` |

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

- **Triggers → dispatch**: GitHub App webhook (`infra/dispatch/github_webhook.py`) and Asana webhook (`asana_webhook.py`) verify HMAC signatures and async-invoke the Dispatch Router. No per-repo GitHub Actions workflow.
- **Onboarding an agent (capability)**: admin clicks Onboard in the dashboard → a `capability#<id>` row is written → the shared `sdlc-agent-builder-<stage>` CodeBuild project builds `agents/<name>` → a build-completion event invokes the `capability-deployer` Lambda, which creates the per-agent runtime IAM role (under IAM path `/sdlc-agents/capabilities/*`, capped by a permissions boundary) + the AgentCore runtime, waits READY, marks the capability `active`.
- **Registry**: the Dispatch Router registry is **rendered from the active capability rows** (`config_store.render_registry`) and written to SSM (`/sdlc-agents/${Stage}/registry`) on every change — replacing the old `.dispatch/agents.yaml` + `sync_registry.py`. Routability keys on "enabled + has a live runtime", so a rebuild never drops a working agent.
- **Weekly security rebuild**: a scheduled Lambda rebuilds every active agent's container so images pick up patches; a failed rebuild leaves the running agent up.
- **Onboarding a repo**: admin onboards `owner/repo` → GitHub App install is verified → the row drives the dispatch allowlist + Cedar repo policy. (Model cost attribution is fleet-wide, not per-repo — see the shared Mantle project above.)

## Conventions

- Agents NEVER close issues, merge PRs, or delete tasks (Cedar-enforced). Work decomposition uses propose → human-approve → execute.
- Custom tools are structured task prompts, not business logic — they return instructions the LLM orchestrates. System prompts live in `prompts.py` beside the agent code.
- The privileged deploy actions (IAM role + runtime + build) live only on event-triggered Lambdas (`capability-deployer`), never on the internet-facing admin API — which holds only `codebuild:StartBuild`.

## Future (low-effort pivots kept open)

- **AWS Agent Registry** as the org-wide agent catalog: the `render_registry`/`publish_registry` seam is isolated so the DynamoDB-rendered registry can additionally sync to Agent Registry (Preview; no CFN yet) without disturbing dispatch.

## Working notes

- Build & verify: dashboard/dispatch/shared Python suites run under `pytest` (moto for AWS); `dashboard/` SPA builds with `npm run build`; validate infra with `sam validate` in `infra/foundation/`.
- `docs/` has the design docs, per-agent specs, and the living **threat model** (`docs/threat-model.md`). `docs/aws-deploy.md` is the full deploy surface.

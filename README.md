# SDLC Agent Fleet

Autonomous AI agents for the software development lifecycle. Each agent handles
a specific role — project management, testing, documentation, business analysis —
and collaborates through a shared dispatch system and memory layer.

Built on [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)
with the [Strands Agents SDK](https://github.com/strands-agents/sdk-python).

![SDLC roles, tools, and tasks across the software lifecycle](docs/assets/sdlc-roles-tools-tasks.png)

Software delivery is a team sport played across many tools by people in diverse
roles. The fleet plants an agent in each role's seat so the paperwork
(decomposition, status, docs, ADR tagging) runs itself and the humans stay
focused on the product.

## Agents

These are the agents that are deployed to AgentCore Runtime today. Additional
agents are in design or early development under [`docs/agents/`](docs/agents/);
they'll be listed here once their code ships.

| Agent | Role | Trigger |
|-------|------|---------|
| [**Workitems**](docs/agents/workitems.md) | PO/PM — work decomposition, status reports, risk detection, sync | `@workitems` in Asana/GitHub |
| [**Researcher**](docs/agents/researcher.md) | Business analyst — research synthesis, competitive intel | `@researcher` |
| [**Docwriter**](docs/agents/docwriter.md) | Technical writer — API docs, user guides, release notes | `@docwriter` |
| [**Adr**](docs/agents/adr.md) | ADR linker — tags issues and reviews PRs against the repo's ADR library | `@adr` on a GitHub issue or PR |

![Workitems decomposing an Asana task after an @mention](docs/assets/at-workitems-mention-asana.png)

![Researcher replying to an @mention on an Asana task](docs/assets/at-researcher-mention-asana.png)

## How It Works

1. A user assigns work via `@agent` mention in Asana, GitHub, or Slack (or a Slack slash command)
2. A signature-verified **webhook** (GitHub App HMAC, Asana HMAC, or Slack `v0` — Slack is `DeploySlack`-gated) async-invokes the **Dispatch Router**, which resolves the mention, applies a prompt-injection guardrail, **authorizes the trigger via Amazon Verified Permissions** (the `TriggerPolicyStore`; fail-closed), and routes to the agent
3. The agent runs on **AgentCore Runtime** (model calls via **Bedrock Mantle**), reaching GitHub/Asana only through the **AgentCore Gateway** (Cedar-enforced, gateway-only)
4. Results are posted back to the originating platform

For Workitems' work decomposition flow:
- User assigns Workitems to an Asana task
- Workitems reads the task, project context, and existing GitHub issues
- Workitems proposes a plan as an Asana comment
- User replies "approved" → Workitems creates the GitHub issues
- User replies with feedback → Workitems revises and re-proposes

![Workitems handing an approved task off to Claude Code on GitHub](docs/assets/workitems-assign-to-claude-github.png)

## Project Structure

```
agents/
  workitems/     Strands agent — PO/PM
  docwriter/     Strands agent — Technical writer
  researcher/    Strands agent — Business analyst
  adr/           Strands agent — ADR linker
  shared/        Shared tools and helpers
dashboard/       Fleet monitoring SPA (React + Vite)
infra/
  dashboard/     Dashboard query + admin API, connectors, AVP authz (Lambda)
  dispatch/      Dispatch Router + GitHub/Asana/Slack webhooks + trigger authz (Lambda)
  foundation/    Shared AWS resources (DynamoDB, S3, IAM, AVP policy stores)
cedar/           Cedar policy guardrails
skills/          Claude Code skills that drive the Quickstart
scripts/         Operator helpers (base-platform deploy, OAuth/webhook bootstrap)
.github/
  workflows/     Lint + security scans only (triggers + deploy are server-side)
docs/            Specs, roadmap, planning docs
```

## Prerequisites

- AWS account with Bedrock model access enabled for the fleet's Mantle model (`anthropic.claude-sonnet-5`) in your target region
- The `AWS::BedrockMantle::Project` CloudFormation resource type activated once per account+region (`aws cloudformation activate-type --type RESOURCE --type-name AWS::BedrockMantle::Project`) — the base deploy provisions the fleet's shared cost-attribution project by default (`DeployMantleProject=true`; set `false` to skip). See `docs/aws-deploy.md` §2.2.
- AWS CLI configured with appropriate credentials
- [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Docker (for building agent containers)
- Python 3.12+

## Quickstart (skill-driven)

The fastest path to a working fleet is to let the bundled Claude Code skill drive
the install. It has a conversation with you to figure out which tools you use
(GitHub/GitLab, Asana/Jira, Slack, etc.), which subset of agents to deploy,
**which AWS account and region to deploy into**, and then walks each provisioning
step end-to-end.

This repo is the **installer**, not the target of the install. Run Claude Code
from a clone of this repo and pass the target project — the repo whose CI/CD
you're wiring the fleet into — as an argument:

```
# a local clone you already have
/sdlc-agents ~/code/my-product

# or a git remote URL (the skill will ask where to clone it)
/sdlc-agents git@github.com:my-org/my-product.git
```

If you omit the argument, the skill will ask. It won't silently assume the cwd
is the target, because ADR detection, `.github/workflows/` inspection, and
`.sdlc-agents/selection.yaml` all need to run against the target repo, not the
installer.

`/sdlc-agents` is a project slash command shipped in this repo under
[`.claude/commands/sdlc-agents.md`](.claude/commands/sdlc-agents.md) — Claude
Code picks it up automatically when you run it from inside a clone. It loads
the install flow from [`skills/sdlc-agents/SKILL.md`](skills/sdlc-agents/SKILL.md),
which delegates to the narrower `skills/sdlc-agents-*/SKILL.md` skills as the
conversation progresses.

The skill will:

1. Ask which PM tool, SCM, and chat platform you use
2. Propose the matching subset of agents (and let you edit)
3. Deploy the base platform (foundation stack, shared build pipeline, dashboard) in the account/region you chose
4. Walk through OAuth/App connections for each integration
5. Onboard each chosen agent in the dashboard Admin view — which builds its container and stands up its AgentCore runtime
6. Register webhooks and bot accounts
7. Run a smoke test per agent

The region you pick is written to `.sdlc-agents/selection.yaml` and reused by
every downstream step — nothing in the install path is hard-coded to `us-west-2`.

## Setup (interactive bootstrap)

`scripts/bootstrap.py` is a thin, interactive one-time **base** setup, using an
AWS profile you pick:

```bash
python scripts/bootstrap.py            # walks you through it
python scripts/bootstrap.py --dry-run  # show the plan first, touch nothing
```

It preflights the required tools (`aws`, `sam`) with install guidance if any are
missing, lets you choose an AWS profile + region (and confirms the account),
collects config (stage, target repo, Asana GIDs — remembered in
`.sdlc-agents/bootstrap.config.json` for re-runs), deploys the foundation stack,
uploads the agent build source, seeds the initial onboarded repos (when the
dashboard is off and there's no Admin UI to do it), and preflights the SSM
secrets (pointing you at the bootstrap scripts for any that are missing — it
doesn't handle secrets itself). Everything is idempotent.

It deliberately does **not** create OIDC providers, CI deploy roles, per-agent
runtime roles, or GitHub Actions secrets — that machinery has been retired. It
also does **not** build images or create AgentCore runtimes — that's the
dashboard's job now. Once the base is deployed, open the dashboard, add
operators/admins to the Cognito groups, and **onboard agents + repos from the
Admin view**.

## Setup (manual)

If you'd rather drive the base deploy directly, `scripts/deploy_fleet.py` is the
one-command base-platform deployer. Set `AWS_REGION` (and `AWS_ACCOUNT_ID`) once
in your shell.

```bash
export AWS_REGION=us-west-2          # pick your region
export AWS_ACCOUNT_ID=123456789012   # your 12-digit account ID
```

### 1. Deploy the base platform

```bash
python scripts/deploy_fleet.py --stage dev --region "$AWS_REGION"
```

This runs `sam build`/`sam deploy` on `infra/foundation` (the `dispatch-router`
Lambda, DynamoDB tables, guardrail, SSM registry, the shared build pipeline +
capability deployer/rebuilder, and — when enabled — Cognito + the dashboard
API/CDN and the AgentCore Gateway), zips `agents/` and uploads it as `source.zip`
to the build pipeline's source bucket, and builds/publishes the dashboard SPA.
It's idempotent and preserves the stack's existing parameter values on re-run.
Use `--dry-run` to preview.

The dashboard and Gateway are off by default; enable them on the first deploy
(e.g. `DeployDashboard=true`, `DeployGateway=true` — see `docs/aws-deploy.md`
§2.3) since you need the dashboard Admin UI to onboard agents.

### 2. Add dashboard operators/admins

Onboarding lives in the dashboard's Admin view, gated by the Cognito `admins`
group (there's no self sign-up). Add yourself to `admins`, open the dashboard
(`DashboardUrl` stack output, also printed by the deploy script), and sign in.

### 3. Onboard agents

In the dashboard **Admin view → Capabilities** panel, click **Onboard
capability** and enter the `agent_id` (must match a directory under `agents/` in
the uploaded build source), plus optional description/aliases/env. Onboarding
starts the shared build (`sdlc-agent-builder-<stage>` CodeBuild, `AGENT_NAME`
override), which pushes to ECR; a build-completion EventBridge event then invokes
the `capability-deployer` Lambda to create the per-agent runtime IAM role +
AgentCore runtime, wait for READY, and mark the capability active — which
re-renders the Dispatch Router registry. ECR repos (`sdlc-agents/<agent>`) are
created by the build with `IMMUTABLE` tag mutability; each build uses a fresh
tag.

To add a **new** agent, create `agents/<name>/` (`agent.py`, `prompts.py`,
`tools/`, `Dockerfile`, `requirements.txt`), re-upload the build source
(`python scripts/deploy_fleet.py --skip-foundation --skip-dashboard`), then
onboard it.

### Optional helper scripts

Under `scripts/`:

- `deploy_fleet.py` — base-platform deployer (foundation stack + build-source upload + dashboard SPA); no per-agent steps
- `bootstrap.py` — interactive one-time base setup (see [Setup (interactive bootstrap)](#setup-interactive-bootstrap)); wraps the foundation deploy + build-source upload, seeds initial repos
- `bootstrap_asana_oauth.py` — one-shot OAuth 2.0 dance for the Asana MCP server; stores the refresh token in SSM
- `bootstrap_jira_oauth.py` — same thing for Atlassian/Jira (3LO)
- `bootstrap_asana_webhook.py` — operator-run webhook registration; attaches a temporary `ssm:PutParameter` policy to the webhook Lambda's role so the Asana handshake can persist the shared secret, then removes the policy

Run these only for the integrations you actually use.

## GitHub Actions Workflows

Agents are built and deployed by the dashboard onboarding flow (CodeBuild →
AgentCore), not by GitHub Actions — and `@mention` **dispatch** now arrives via
the fleet's GitHub App **webhook**, not a workflow. The old `agent-dispatch.yml`
and `claude-code.yml` workflows and the OIDC deploy role have been retired. The
workflows that remain in `.github/workflows/` cover lint and repo hygiene only:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `python-lint.yml` | Push/PR to `main` | Ruff lint + format check |
| `ash-security-scan.yml` | Push/PR to `main` | Security scan changed files |
| `ash-security-comment.yml` | After ASH scan | Post scan results to PR |
| `ash-full-repository-scan.yml` | Monthly + manual | Full repo security scan |
| `dependabot.yml` | Dependabot PRs | Auto-merge patch/minor updates |

(The optional Claude Code on Bedrock feature installs its own `claude-code.yml`
into a *target* repo — see the `sdlc-agents-setup-claude-code` skill. That is
separate from the fleet and not shipped in this repo's workflows.)

## Security

This fleet is published as a **reference architecture**, not a turnkey
production system. [`docs/threat-model.md`](docs/threat-model.md) is the
starting place for preparing your own deployment: walk through every
**Accepted** and **Open** finding and make your own risk decisions before
pointing agents at a repository or workspace you care about.

The design assumes a **trusted-contributor** deployment context — issue
authors, task creators, and commenters are already authorized members of the
repository or workspace. Key findings you should understand before shipping:

- **Prompt injection (T-1, T-2, T-3, Partially mitigated)** — LLM agents
  can still be subverted by adversarial issue, task, or comment content, but
  every inbound `@mention` is scored by an
  [Amazon Bedrock Guardrail](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html)
  (`PROMPT_ATTACK` filter) at the Dispatch Router edge, and every agent's
  model call (on the Bedrock Mantle endpoint) carries the same guardrail via
  Mantle headers — catching injection in content the agent fetches later via
  its tools. Guardrails is probabilistic, not deterministic; don't treat it as
  a hard boundary.

  ![Workitems blocking a prompt-attack attempt on an Asana task](docs/assets/workflow-agent-asana-mitigates-prompt-attacks.png)

- **Cedar tool policies (T-5, Partially mitigated)** — `cedar/*.cedar` files
  describe per-agent tool allow/deny rules; the enforced form
  (`infra/dashboard/fleet_policy.py`) runs in the AgentCore Gateway policy
  engine in the invocation path (the fleet is gateway-only). The engine is
  rolled out `LOG_ONLY` first, so until an operator flips it to `ACTIVE`,
  tool-grant deny decisions log rather than block (the co-repo interceptor and
  per-call scoped GitHub credential enforce regardless).
- **Legitimate-path exfiltration (T-15, Accepted)** — a subverted agent can
  leak context through its own write-capable tools (GitHub comment, Asana
  task). The Gateway Cedar engine bounds which tools an agent can reach; content
  filtering of tool *outputs* is not attempted.

Mitigated surfaces include Dispatch Router IAM scope (T-6), the GitHub App
webhook HMAC trigger (T-30, which replaced the retired OIDC path T-7), the
bounded per-owner GitHub App credential (T-11), AVP-authorized dashboard API
(T-29), AVP data-driven trigger authorization (T-4, T-40; fail-closed,
default-deny — replacing the removed per-capability allowlist), the Slack
connector's signature/replay/bot-loop controls (T-32–T-38), the isolated
capability-deployer privileged IAM (T-31), ECR image immutability (T-19), and
Asana webhook credential hygiene (T-8, T-9). See the threat model for the full
matrix.

### Before deploying against real repositories

The main **Open** finding that does not block the reference architecture but
should be handled before you wire the fleet to a production repo or workspace:

- **T-13 — API Gateway throttling.** The public webhook endpoints (Asana, the
  GitHub App, and — when enabled — Slack) ship without usage-plan throttling or
  WAF. Attach an API Gateway usage plan (burst + steady-state limits) and, if the
  endpoints are discoverable, an AWS WAF web ACL with an IP-based rate rule before
  exposing them to untrusted inbound traffic.

## Docs

- [Roadmap](docs/roadmap.md) — shipped, near-term, ideas
- [Threat Model](docs/threat-model.md) — threats, current controls, recommended next work
- [AWS Deploy Surface](docs/aws-deploy.md) — what the project provisions and what's required for a deterministic deploy
- [Dashboard](dashboard/README.md) — fleet monitoring SPA: local dev, architecture, deploy
- [PRFAQ](docs/01-prfaq-agent-fleet.md) — framing and FAQ
- [PRD](docs/02-prd-agent-fleet.md) — requirements (Shipped vs. Roadmap per item)
- [Design](docs/03-design-agent-fleet.md) — system architecture as shipped
- [Status](docs/agent-fleet-implementation-plan.md) — what shipped, what didn't, what's next
- [Per-agent docs](docs/agents/) — Workitems, Researcher, Docwriter, Adr
- [Specs](docs/specs/) — detailed specs for each shipping agent + the Dispatch routing layer

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

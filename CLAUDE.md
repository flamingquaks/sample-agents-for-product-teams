# PDLC Agent Fleet

Autonomous AI agents for the software development lifecycle, deployed on
Amazon Bedrock AgentCore.

## Project Structure

```
agents/           — Agent code (Strands SDK, containerized, deployed to AgentCore Runtime)
  workitems/      — PO/PM agent: work decomposition, status, risk, sync
  researcher/     — BA agent: research synthesis, competitive intel, backlog
  docwriter/      — Tech-writer agent: API docs, guides, release notes
  shared/         — Shared tools and helpers used by all agents
infra/            — AWS infrastructure (SAM/CloudFormation)
  dispatch/       — Dispatch Router + Asana webhook receiver (Lambda)
  foundation/     — Shared resources (DynamoDB, S3, IAM, CloudWatch)
cedar/            — Cedar policies (guardrails for agent tool access)
docs/             — Planning docs, specs, roadmap
  specs/          — Individual agent and system specs
.dispatch/        — Agent registry (agents.yaml)
.github/workflows — CI/CD pipelines and GitHub event triggers
```

## Tech Stack

- **Language**: Python 3.12
- **Agent Framework**: Strands Agents SDK
- **Model**: Claude Opus 4.7 via Amazon Bedrock
- **Runtime**: Amazon Bedrock AgentCore Runtime (containerized)
- **Tool Access**: **Gateway-only.** Every agent routes ALL MCP tool calls through the **AgentCore Gateway** (`GATEWAY_MCP_URL` required; the agent refuses to start without it) — one SigV4-signed client via the runtime role (`agents/shared/tools/gateway.py`). The gateway fronts Asana (direct MCP target) and GitHub (via the **SCM broker Lambda**, `infra/dispatch/scm_broker.py`), enforces a Cedar policy engine, and runs the **SCM co-repo REQUEST interceptor** (`infra/dispatch/scm_interceptor.py`). No direct-to-vendor path — that would bypass policy + observability.
- **Auth**: Asana via OAuth/PAT in SSM (SecureString). **GitHub via per-owner GitHub App installation tokens minted server-side** by the broker/interceptor/reply Lambdas (the shared PAT was retired; agents hold NO GitHub credential). The App private key is in Secrets Manager, app id/slug in SSM String. Tokens are scoped per call to the co-reachable repo set (co-repo grouping) + the per-agent∩per-tool permission tier. Agent runtime roles get only `bedrock-agentcore:InvokeGateway`. AgentCore Identity is a planned upgrade.
- **Multi-repo**: multi-owner (mixed personal + org); each repo declares which OTHER repos a dispatch from it may act on (`co_repo_mode`: isolated | group | all); per-agent product access (docwriter opens PRs + writes code, workitems issues-only, adr comment/read-only, researcher no GitHub). Enforced at the interceptor + credential layers — see `docs/specs/github-onboarding-spec.md` §3.7.
- **Memory**: Agents honor `AGENTCORE_MEMORY_ID` via Strands' `AgentCoreMemoryToolProvider`. No Memory resource is provisioned by the fleet's infra template today; this is a roadmap item.
- **Policy**: Cedar policy files under `cedar/<agent>.cedar` (advisory unless the Gateway is deployed). With `DeployGateway`, the AgentCore Gateway policy engine enforces Cedar in the invocation path — including the admin repo-allowlist policy (`infra/dashboard/fleet_policy.py`).
- **Infra**: AWS SAM (CloudFormation) — see `docs/aws-deploy.md` for the full surface.
- **CI/CD**: GitHub Actions → ECR → AgentCore Runtime

## Conventions

- Agents NEVER close issues, merge PRs, or delete tasks. Cedar policies enforce this.
- Work decomposition uses the approval pattern: agent proposes → human approves → agent executes.
- Custom tools are structured task prompts, not business logic. They return instructions that guide the agent's reasoning. The LLM does the actual orchestration.
- System prompts live in `prompts.py` alongside agent code, not in separate config.
- Agent registry lives in `.dispatch/agents.yaml` and is synced to SSM on deploy.

## Working with Agents

Each agent under `agents/` is self-contained:
- `agent.py` — Strands agent entry point with `@app.entrypoint`
- `prompts.py` — System prompt (versioned with code)
- `tools/` — Custom `@tool` functions
- `tests/eval_dataset.json` — Golden set for quality evaluation
- `Dockerfile` + `requirements.txt` — Container deployment

## Current Focus

Building the Workitems agent (MLP). See `docs/roadmap.md` for the full plan.

# AGENTS.md

The canonical project reference for this repo is **[CLAUDE.md](./CLAUDE.md)** —
read it. It covers what the fleet is, the architecture (Mantle models, AVP API
authz, gateway-only tools, webhook triggers), the project layout, how onboarding
and dispatch flow, and the conventions to follow.

This file exists so agents and tools that look for `AGENTS.md` land on the same
guidance; it intentionally does not duplicate content — CLAUDE.md is the single
source of truth.

## Deploy & onboarding, in one paragraph

Deploy the **base platform** once with `python scripts/deploy_fleet.py`
(foundation stack + agent build source + dashboard SPA); `scripts/bootstrap.py`
does the one-time privileged setup. After that, **everything is self-service in
the dashboard Admin view** — onboard an agent (the shared CodeBuild pipeline
builds its container and the `capability-deployer` Lambda stands up its AgentCore
runtime), onboard the repos it may act on, register the GitHub App (its webhook
delivers `@mention` events; no per-repo workflow), and manage settings. There is
no GitHub-OIDC / CI deploy path. Full deploy surface: `docs/aws-deploy.md`.

## Pointers

- Architecture, stack, conventions → **[CLAUDE.md](./CLAUDE.md)**
- Deploy surface & runbook → `docs/aws-deploy.md`
- Threat model → `docs/threat-model.md`
- Per-agent + system specs → `docs/specs/`

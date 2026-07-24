"""Project configuration for the Docwriter agent.

Maps project resources so the agent always knows where to find things.
Loaded at startup and injected into the system prompt.

The fleet is multi-repo: the GitHub repo an agent acts on is NOT baked at
deploy time — it comes from the dispatch (``source_context.repo``) and is passed
into ``build_project_context`` per invocation. Asana project config stays
deploy-time env.
"""

import os

# Asana — reads tasks for feature context, posts doc status. Optional: a
# UI-onboarded capability may carry no Asana env (the capability row's `env`
# map is often empty), and the agent must still boot — a missing project just
# means no Asana scope for this deployment.
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "")
ASANA_PROJECT_NAME = os.environ.get("ASANA_PROJECT_NAME", "")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")


def _github_section(github_repo: str | None) -> str:
    """Render the GitHub resource block for the dispatched repo.

    ``github_repo`` is the ``owner/repo`` from the current dispatch. When it's
    present we pin the agent to it; when it's absent we defer to the Current
    Dispatch block rather than inventing one."""
    if github_repo and "/" in github_repo:
        owner, name = github_repo.split("/", 1)
        return f"""\
GitHub:
- Repository: {github_repo}
- Owner: {owner}
- Repo name: {name}

When opening doc PRs, ALWAYS target repo "{github_repo}" — the repository this \
request came from. Do NOT target any other repo."""
    return """\
GitHub:
- Repository: taken from the Current Dispatch section below (the repo this \
request came from).

When opening doc PRs, ALWAYS target the repository named in the Current \
Dispatch section. Never invent or assume a different repo."""


def build_project_context(github_repo: str | None = None) -> str:
    """Build a project context block for injection into the system prompt.

    ``github_repo`` is the dispatched repo (``source_context.repo``)."""
    if not ASANA_PROJECT_GID:
        return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project IDs.

{_github_section(github_repo)}

No Asana project is configured for this deployment — take feature context
from the dispatch and the repo instead of Asana tasks.
"""
    project_line = f"- Project: {ASANA_PROJECT_NAME}\n" if ASANA_PROJECT_NAME else ""
    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project IDs.

{_github_section(github_repo)}

Asana:
{project_line}- Project GID: {ASANA_PROJECT_GID}
- Workspace GID: {ASANA_WORKSPACE_GID}

When reading Asana tasks for feature context, use project "{ASANA_PROJECT_GID}".
Do NOT ask the user for these — you already have them.
"""

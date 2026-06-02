"""Project configuration for the Workitems agent.

Maps project resources so the agent always knows where to find things.
Loaded at startup and injected into the system prompt.

The PM (planning) backend is selectable via PM_BACKEND:
- "asana"  (default): Asana is the planning surface, GitHub the development
  surface. Requires the ASANA_* and GITHUB_REPO env vars.
- "github": GitHub is BOTH surfaces — Issues for work items and a Projects V2
  board for planning/status. Requires GITHUB_REPO and the GITHUB_PROJECT_* env
  vars; no Asana configuration is read.

Required env vars are read lazily inside build_project_context() per backend,
so a GitHub-only deployment never touches the Asana vars (and vice versa).
Reading them at import time would crash the agent at startup whenever the
*other* backend's vars are absent.
"""

import os

PM_BACKEND = os.environ.get("PM_BACKEND", "asana").lower()


def _require(name: str) -> str:
    """Return a required env var, or raise a clear, backend-scoped error."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"PM_BACKEND={PM_BACKEND!r} requires the {name} environment variable, "
            f"but it is not set. Configure it on the workitems runtime."
        )
    return val


def _build_asana_context() -> str:
    project_gid = _require("ASANA_PROJECT_GID")
    workspace_gid = _require("ASANA_WORKSPACE_GID")
    project_name = os.environ.get("ASANA_PROJECT_NAME", "")
    github_repo = _require("GITHUB_REPO")
    owner, repo_name = github_repo.split("/", 1)

    project_line = f"- Project: {project_name}\n" if project_name else ""
    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project IDs.

Asana:
{project_line}- Project GID: {project_gid}
- Workspace GID: {workspace_gid}

GitHub:
- Repository: {github_repo}
- Owner: {owner}
- Repo name: {repo_name}

When creating GitHub issues, ALWAYS use repo "{github_repo}".
When reading Asana tasks, ALWAYS use project "{project_gid}".
Do NOT ask the user for these — you already have them.
"""


def _build_github_context() -> str:
    github_repo = _require("GITHUB_REPO")
    owner, repo_name = github_repo.split("/", 1)
    project_number = _require("GITHUB_PROJECT_NUMBER")
    # The Projects V2 board can be owned by an org or user that differs from the
    # repo owner, and can span repos. Default the board owner to the repo owner
    # but allow an explicit override.
    project_owner = os.environ.get("GITHUB_PROJECT_OWNER", owner)

    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project numbers.

GitHub (planning AND development):
- Repository: {github_repo}
- Owner: {owner}
- Repo name: {repo_name}
- Projects V2 board: project #{project_number} owned by "{project_owner}"

The Projects V2 board (#{project_number}) is the roadmap the product owner
reads. Work items are GitHub Issues added to that board; planning and status
live in the board's status columns and status updates.

When creating GitHub issues, ALWAYS use repo "{github_repo}".
When reading or updating the board, ALWAYS use project #{project_number}
owned by "{project_owner}".
Do NOT ask the user for these — you already have them.
"""


def build_project_context() -> str:
    """Build a project context block for injection into the system prompt.

    Branches on PM_BACKEND so only the selected backend's env vars are read.
    """
    if PM_BACKEND == "github":
        return _build_github_context()
    if PM_BACKEND == "asana":
        return _build_asana_context()
    raise RuntimeError(
        f"Unknown PM_BACKEND={PM_BACKEND!r}. Supported values: 'asana', 'github'."
    )

"""Project configuration for the Researcher agent.

Maps project resources so the agent always knows where to find things.
Loaded at startup and injected into the system prompt.

The PM backend is selectable via PM_BACKEND:
- "asana"  (default): Asana is the research workspace — input comes from Asana
  tasks, output goes back as Asana comments / new tasks. Requires ASANA_*.
- "github": GitHub is the workspace — input comes from GitHub issues and a
  Projects V2 board, output goes back as issue comments / new issues on the
  board. Requires GITHUB_REPO and GITHUB_PROJECT_NUMBER (+ optional
  GITHUB_PROJECT_OWNER); no Asana configuration is read.

Required env vars are read lazily per backend, so a GitHub-only deployment
never touches the Asana vars (reading them at import time would crash the
agent at startup whenever the other backend's vars are absent).
"""

import os

PM_BACKEND = os.environ.get("PM_BACKEND", "asana").lower()


def _require(name: str) -> str:
    """Return a required env var, or raise a clear, backend-scoped error."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"PM_BACKEND={PM_BACKEND!r} requires the {name} environment variable, "
            f"but it is not set. Configure it on the researcher runtime."
        )
    return val


def _build_asana_context() -> str:
    project_gid = _require("ASANA_PROJECT_GID")
    workspace_gid = _require("ASANA_WORKSPACE_GID")
    project_name = os.environ.get("ASANA_PROJECT_NAME", "")

    project_line = f"- Project: {project_name}\n" if project_name else ""
    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for project IDs.

Asana:
{project_line}- Project GID: {project_gid}
- Workspace GID: {workspace_gid}

When reading or creating tasks, ALWAYS use project "{project_gid}".
Do NOT ask the user for these — you already have them.
"""


def _build_github_context() -> str:
    github_repo = _require("GITHUB_REPO")
    owner, repo_name = github_repo.split("/", 1)
    project_number = _require("GITHUB_PROJECT_NUMBER")
    project_owner = os.environ.get("GITHUB_PROJECT_OWNER", owner)

    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project numbers.

GitHub:
- Repository: {github_repo}
- Owner: {owner}
- Repo name: {repo_name}
- Projects V2 board: project #{project_number} owned by "{project_owner}"

Research input comes from GitHub issues and the Projects V2 board (#{project_number}).
Output goes back as issue comments, and — when you draft user stories — as new
GitHub issues added to that board.

When creating issues, ALWAYS use repo "{github_repo}".
When reading or updating the board, use project #{project_number} owned by "{project_owner}".
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

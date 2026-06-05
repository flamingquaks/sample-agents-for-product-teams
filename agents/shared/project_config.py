"""Shared PM-backend scaffold for per-agent project_config.

PM-capable agents (workitems, researcher) select their planning backend via the
PM_BACKEND env var and read backend-specific env vars **lazily** (reading them at
import time crashes the agent at startup whenever the *other* backend's vars are
absent — e.g. a GitHub-only deploy has no ASANA_* set).

This module owns the parts that are identical across those agents:
- `PM_BACKEND` (read once)
- `require_env(name, runtime)` — fetch a required env var or raise a clear,
  backend-scoped error
- `build_project_context(asana_builder, github_builder)` — the asana/github
  dispatch (+ unknown-backend guard)

Each agent keeps only its two prose-builder functions (the context text differs
per role) and calls `build_project_context(...)` with them.
"""

import os

PM_BACKEND = os.environ.get("PM_BACKEND", "asana").lower()


def require_env(name: str, runtime: str) -> str:
    """Return a required env var, or raise a clear, backend-scoped error.

    Args:
        name: the environment variable name.
        runtime: the agent runtime name, for the error message (e.g. "workitems").
    """
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"PM_BACKEND={PM_BACKEND!r} requires the {name} environment variable, "
            f"but it is not set. Configure it on the {runtime} runtime."
        )
    return val


def build_project_context(asana_builder, github_builder) -> str:
    """Dispatch to the agent's backend-specific context builder.

    Args:
        asana_builder: zero-arg callable returning the asana-mode context block.
        github_builder: zero-arg callable returning the github-mode context block.

    Only the selected backend's builder runs, so only that backend's env vars
    are read.
    """
    if PM_BACKEND == "github":
        return github_builder()
    if PM_BACKEND == "asana":
        return asana_builder()
    raise RuntimeError(
        f"Unknown PM_BACKEND={PM_BACKEND!r}. Supported values: 'asana', 'github'."
    )

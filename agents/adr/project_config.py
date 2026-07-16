"""Project configuration for the Adr agent.

Unlike the other agents, Adr's scope is GitHub-only and its per-repo
behavior (which ADR directory to read, which statuses count) comes from
`.sdlc-agents/adr.yaml` at read time, not from startup config.

The fleet is multi-repo: the GitHub repo Adr acts on is NOT baked at deploy
time — it comes from the dispatch (``source_context.repo``) and is passed into
``build_project_context`` per invocation.
"""

# ADR discovery — search these paths in order when the repo has no
# `.sdlc-agents/adr.yaml` override. First hit wins.
DEFAULT_ADR_DIRS = ["docs/adrs", "adrs", "ADRs", "docs/decisions", "architecture/decisions"]

# Matching thresholds — overridable per repo via config file
DEFAULT_CONFIDENCE_THRESHOLD = 0.65
DEFAULT_REQUIRE_STATUS = "accepted"  # or "any"


def _github_section(github_repo: str | None) -> str:
    """Render the GitHub resource block for the dispatched repo."""
    if github_repo and "/" in github_repo:
        owner, name = github_repo.split("/", 1)
        return f"""\
GitHub:
- Repository: {github_repo}
- Owner: {owner}
- Repo name: {name}"""
    return """\
GitHub:
- Repository: taken from the Current Dispatch section below (the repo this \
request came from). Operate ONLY on that repository."""


def build_project_context(github_repo: str | None = None) -> str:
    """Build a project context block for injection into the system prompt.

    ``github_repo`` is the dispatched repo (``source_context.repo``)."""
    return f"""\

## Project Resources

{_github_section(github_repo)}

ADR discovery (in order):
- `.sdlc-agents/adr.yaml` → `adr_dir` key (if the file exists)
- Otherwise try these paths and use the first one that exists:
  {", ".join(DEFAULT_ADR_DIRS)}

If no ADR directory is found, post a single "no ADRs found" comment and stop.
Do not try to operate without ADRs.
"""

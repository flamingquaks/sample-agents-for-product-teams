"""Durable git workspace for repo-capable agents (durable-repo-work spec, Phase 1).

The SCM broker gives agents API-shaped repo access (read a file, push a blob) but
no working tree — no clone, no build/test loop. This module is the working-tree
side: real ``git`` against an EPHEMERAL workspace (D1 — disk is scratch), with
durability provided by GitHub itself (D3 — in-progress work lives on a
``wip/<assignment_id>`` branch, one per repo; nothing else survives the
container).

Credential model (T-4 preserved): the agent holds NO GitHub credential. Each git
network operation mints a short-lived, repo-scoped GitHub App installation token
by invoking the **workspace token vendor** Lambda (runtime code — the model can
name a repo but never sees or handles the token), and passes it to git via a
per-command ``http.extraheader`` config flag. ``-c`` config is process-scoped:
the token is never written to the environment, ``.git/config``, or disk.

Scoping: the vendor enforces the same boundaries as the SCM broker — the repo
must be onboarded + co-reachable from the dispatch ORIGIN (server truth, stamped
by runtime code from the dispatch payload), and the token is bounded by the
agent's GitHub permission tier. A repo outside the dispatch's co-repo group
fails closed at the vendor, exactly like a gateway tool call would.

Model-visible tools (Strands ``@tool``): ``clone_repo``, ``workspace_run``,
``commit_and_push``. Runtime-only functions (never exposed to the model):
``configure``, ``push_all_clean``, ``snapshot``, ``restore``.
"""

import base64
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from strands import tool

logger = logging.getLogger(__name__)

# Branch prefix for in-progress work (D3). One branch per (assignment, repo).
WIP_PREFIX = "wip/"

_GIT_TIMEOUT_SECONDS = 300
_RUN_TIMEOUT_SECONDS = 600
_RUN_OUTPUT_MAX_CHARS = 20_000

# Env var prefixes scrubbed from workspace_run subprocesses: build/test commands
# are model-directed, so the runtime role's AWS credentials and any ambient
# tokens must not be reachable from them (no-credential-at-rest posture).
_SCRUBBED_ENV_PREFIXES = ("AWS_", "GITHUB_", "SLACK_", "ASANA_")


class WorkspaceError(RuntimeError):
    """A git/vendor operation failed in a way the caller must see."""


@dataclass
class _State:
    """Per-invocation dispatch binding — set by runtime code BEFORE the model
    runs (the model controls tool arguments, never this)."""

    assignment_id: str = ""
    agent_id: str = ""
    origin: str = ""
    cloned: dict = field(default_factory=dict)  # repo -> Path
    # Last-known remote tip of each repo's wip branch ("" = branch absent).
    # Load-bearing for pushes: shallow clones are single-branch, so git has NO
    # origin/wip/<id> remote-tracking ref and a bare --force-with-lease can
    # never take the lease ("stale info" on every push after the first). We
    # therefore lease against THIS explicitly tracked sha.
    remote_shas: dict = field(default_factory=dict)  # repo -> sha


_state = _State()

_lambda_client = None


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        import boto3

        _lambda_client = boto3.client("lambda")
    return _lambda_client


def configure(*, assignment_id: str, agent_id: str, origin: str) -> None:
    """Bind this invocation's dispatch identity (runtime code only). Resets any
    prior clone bookkeeping — a fresh invoke starts with an empty workspace."""
    _state.assignment_id = assignment_id or ""
    _state.agent_id = agent_id or ""
    _state.origin = origin or ""
    _state.cloned = {}
    _state.remote_shas = {}


def enabled() -> bool:
    """Whether the durable workspace is available in this runtime: needs git in
    the image, the token-vendor function configured, and a real assignment."""
    return bool(
        os.environ.get("WORKSPACE_TOKEN_FUNCTION")
        and _state.assignment_id
        and shutil.which("git")
    )


def wip_branch() -> str:
    return f"{WIP_PREFIX}{_state.assignment_id}"


def workspace_root() -> Path:
    """The assignment's scratch root. /tmp is writable for the nologin agent
    user; everything under it is D1-ephemeral."""
    root = os.environ.get("WORKSPACE_ROOT", "/tmp/work")
    return Path(root) / _state.assignment_id


def _repo_dir(repo: str) -> Path:
    return workspace_root() / repo.replace("/", "__")


def _mint_token(repo: str, *, write: bool) -> str:
    """A short-lived, repo-scoped GitHub App token from the vendor Lambda.
    Runtime-code-only; the vendor enforces onboarding + co-repo grouping from
    the dispatch origin and the agent's permission tier."""
    function = os.environ.get("WORKSPACE_TOKEN_FUNCTION", "")
    if not function:
        raise WorkspaceError("workspace token vendor is not configured")
    payload = {
        "repo": repo,
        "agent": _state.agent_id,
        "origin": _state.origin,
        "write": bool(write),
    }
    resp = _lambda().invoke(
        FunctionName=function,
        Payload=json.dumps(payload).encode(),
    )
    try:
        body = json.loads(resp["Payload"].read())
    except (ValueError, KeyError) as exc:
        raise WorkspaceError("token vendor returned an unreadable response") from exc
    if not isinstance(body, dict) or body.get("error") or not body.get("token"):
        reason = (body or {}).get("error", "no token in response") if isinstance(body, dict) else "bad response"
        raise WorkspaceError(f"could not obtain a workspace credential for {repo}: {reason}")
    return body["token"]


def _auth_flags(token: str) -> list[str]:
    """Per-command git config carrying the credential. ``-c`` never persists —
    the token exists only in this process's argv for the one operation."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


def _git(args: list[str], *, cwd: Path | None = None, token: str | None = None) -> str:
    """Run one git command; returns stdout. Raises WorkspaceError on failure with
    stderr attached (token material never appears in stderr — it lives in a
    config flag git doesn't echo)."""
    cmd = ["git", *(_auth_flags(token) if token else []), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def _remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _head_sha(repo_dir: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=repo_dir).strip()


def _remote_branch_sha(repo: str, branch: str, token: str) -> str:
    """The remote tip of ``branch``, or '' if the branch doesn't exist."""
    out = _git(["ls-remote", _remote_url(repo), f"refs/heads/{branch}"], token=token)
    return out.split()[0] if out.strip() else ""


def _clone(repo: str, *, ref: str = "", branch: str = "") -> Path:
    """Clone ``repo`` shallow into the workspace; checkout the wip branch when it
    exists remotely (resume), else stay on the default/requested branch. With
    ``ref``, fetch + checkout that exact sha (workspace_snapshot restore)."""
    dest = _repo_dir(repo)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = _mint_token(repo, write=False)
    clone_args = ["clone", "--depth", "50", _remote_url(repo), str(dest)]
    if branch:
        clone_args[1:1] = ["--branch", branch]
    _git(clone_args, token=token)
    # Identity for commits made in this workspace.
    _git(["config", "user.name", f"sdlc-agent[{_state.agent_id or 'fleet'}]"], cwd=dest)
    _git(["config", "user.email", "sdlc-agents@noreply.github.com"], cwd=dest)

    wip = wip_branch()
    wip_sha = _remote_branch_sha(repo, wip, token)
    if ref:
        # Snapshot restore: fetch the exact recorded sha (GitHub serves
        # reachable-sha fetches) and put the wip branch on it.
        _git(["fetch", "--depth", "50", "origin", ref], cwd=dest, token=token)
        _git(["checkout", "-B", wip, ref], cwd=dest)
    elif wip_sha:
        _git(["fetch", "--depth", "50", "origin", f"{wip}:{wip}"], cwd=dest, token=token)
        _git(["checkout", wip], cwd=dest)
    else:
        _git(["checkout", "-b", wip], cwd=dest)
    _state.cloned[repo] = dest
    # Seed the lease base: the remote wip tip as of this clone ("" = absent).
    _state.remote_shas[repo] = wip_sha
    return dest


def _commit_if_dirty(repo_dir: Path, message: str) -> bool:
    """Stage + commit everything if the tree is dirty. True if a commit landed."""
    status = _git(["status", "--porcelain"], cwd=repo_dir)
    if not status.strip():
        return False
    _git(["add", "-A"], cwd=repo_dir)
    _git(["commit", "-m", message], cwd=repo_dir)
    return True


def _head_changed_files(repo_dir: Path) -> list[str]:
    """The files HEAD touched (relative paths). Best-effort — used only for
    the run record's commit bookkeeping, never for git logic."""
    try:
        out = _git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=repo_dir,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except WorkspaceError:
        return []


def _record_pushed_commit(repo: str, repo_dir: Path, sha: str, message: str) -> None:
    """Append this pushed commit (+ changed files) to the assignment's run
    record so the dashboard shows exactly what the run changed. Best-effort."""
    try:
        from shared.assignment import record_commit

        record_commit(
            _state.assignment_id,
            repo=repo,
            branch=wip_branch(),
            sha=sha,
            message=message,
            files=_head_changed_files(repo_dir),
        )
    except Exception:  # noqa: BLE001 — bookkeeping never fails a push
        logger.exception("could not record pushed commit %s@%s", repo, sha[:12])


def _push(repo: str, repo_dir: Path) -> str:
    """Push the wip branch and VERIFY the remote tip matches local HEAD (D7 —
    a pause checkpoint must be provably durable). Returns the pushed sha.

    The lease is EXPLICIT (``--force-with-lease=<ref>:<expected>``): shallow
    clones are single-branch, so no origin/wip remote-tracking ref exists and
    the bare flag would reject every push after the branch exists ("stale
    info"). We lease against the sha we last observed on the remote (seeded at
    clone, advanced after each successful push) — same safety property (a
    concurrent foreign push loses us the lease and we fail loud) without
    depending on remote-tracking state a shallow clone never has."""
    token = _mint_token(repo, write=True)
    wip = wip_branch()
    expected = _state.remote_shas.get(repo, "")
    lease = f"refs/heads/{wip}:{expected}" if expected else f"refs/heads/{wip}:"
    _git(
        ["push", f"--force-with-lease={lease}", "origin", f"{wip}:{wip}"],
        cwd=repo_dir,
        token=token,
    )
    local = _head_sha(repo_dir)
    remote = _remote_branch_sha(repo, wip, token)
    if remote != local:
        raise WorkspaceError(
            f"push verification failed for {repo}: local {local[:12]} != remote {remote[:12] or '(missing)'}"
        )
    _state.remote_shas[repo] = local
    return local


# --- model-visible tools ------------------------------------------------------


@tool
def clone_repo(repo: str) -> str:
    """Clone a repository into your working directory for real file work
    (editing across files, running builds/tests). Only repositories in your
    dispatch's approved scope are clonable. For reading a single file, prefer
    the get_file_contents tool — no clone needed.

    Args:
        repo: The repository as "owner/name".

    Returns:
        The local path of the working tree and the branch you are on.
    """
    try:
        dest = _clone(repo)
        return (
            f"Cloned {repo} to {dest} on branch {wip_branch()}. "
            "Make your edits with file operations under that path, run builds/tests "
            "with workspace_run, then commit_and_push to checkpoint your work."
        )
    except WorkspaceError as exc:
        return f"ERROR: {exc}"


@tool
def workspace_run(command: str, repo: str) -> str:
    """Run a shell command (build, test, lint) inside a cloned repository's
    working tree. The repository must have been cloned with clone_repo first.

    Args:
        command: The shell command to run (e.g. "pytest -q", "npm test").
        repo: The repository ("owner/name") whose working tree to run in.

    Returns:
        Exit status plus captured stdout/stderr (truncated when long).
    """
    repo_dir = _state.cloned.get(repo)
    if not repo_dir:
        return f"ERROR: {repo} is not cloned — call clone_repo first."
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_SCRUBBED_ENV_PREFIXES)
    }
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {_RUN_TIMEOUT_SECONDS}s."
    out = (proc.stdout or "") + (("\n--- stderr ---\n" + proc.stderr) if proc.stderr else "")
    if len(out) > _RUN_OUTPUT_MAX_CHARS:
        out = out[:_RUN_OUTPUT_MAX_CHARS] + "\n…[output truncated]"
    return f"exit code {proc.returncode}\n{out}"


@tool
def commit_and_push(repo: str, message: str) -> str:
    """Commit all current changes in a cloned repository and push them to your
    work-in-progress branch on GitHub. Call this at every meaningful milestone —
    pushed work survives anything; unpushed work is lost if this session ends
    unexpectedly.

    Args:
        repo: The repository ("owner/name") to commit in.
        message: The commit message describing the change.

    Returns:
        The pushed commit sha, or an error to react to.
    """
    repo_dir = _state.cloned.get(repo)
    if not repo_dir:
        return f"ERROR: {repo} is not cloned — call clone_repo first."
    try:
        committed = _commit_if_dirty(repo_dir, message or "wip: agent checkpoint")
        sha = _push(repo, repo_dir)
        if committed:
            _record_pushed_commit(repo, repo_dir, sha, message or "wip: agent checkpoint")
        note = "committed and pushed" if committed else "nothing new to commit; pushed"
        return f"{note} {repo}@{sha[:12]} on {wip_branch()}. Open the PR from this branch when the work is done."
    except WorkspaceError as exc:
        return f"ERROR: {exc}"


MODEL_TOOLS = [clone_repo, workspace_run, commit_and_push]


# --- runtime-only (pause/resume protocol) --------------------------------------


def push_all_clean() -> list[dict]:
    """Pause protocol step 1 (D7): commit + push every cloned repo and verify
    each landed. Raises WorkspaceError if ANY repo can't be made durably clean —
    the caller must then fail loud, never pause-and-lose. Returns the
    workspace_snapshot: [{repo, branch, sha}, ...]."""
    snapshot = []
    for repo, repo_dir in _state.cloned.items():
        committed = _commit_if_dirty(
            repo_dir, "wip: pause checkpoint (awaiting user input)"
        )
        sha = _push(repo, repo_dir)
        if committed:
            _record_pushed_commit(
                repo, repo_dir, sha, "wip: pause checkpoint (awaiting user input)"
            )
        snapshot.append({"repo": repo, "branch": wip_branch(), "sha": sha})
    return snapshot


def restore(workspace_snapshot: list[dict]) -> list[str]:
    """Resume protocol: re-clone each snapshot repo and check out the recorded
    sha on the wip branch. Returns the repos restored. Raises WorkspaceError if
    any restore fails (the conversation would be out of sync with the tree)."""
    restored = []
    for entry in workspace_snapshot or []:
        repo = str(entry.get("repo", ""))
        ref = str(entry.get("sha", ""))
        if not repo or not ref:
            continue
        _clone(repo, ref=ref)
        restored.append(repo)
    return restored

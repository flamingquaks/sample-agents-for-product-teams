"""Tests for the durable git workspace (shared/tools/workspace.py).

Real ``git`` against local bare "remotes" (file:// URLs) — no GitHub, no
Lambda: the token vendor is stubbed and ``_remote_url`` is pointed at the local
bare repo, so clone/commit/push/verify semantics are exercised for real. The
security-relevant assertions (token never persisted, vendor called per network
op, D7 push verification) are covered explicitly.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shared.tools.workspace as ws

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("git") is None, reason="git not installed"
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path):
    """A local bare 'origin' seeded with one commit on main."""
    bare = tmp_path / "origin.git"
    _git(["init", "--bare", "--initial-branch=main", str(bare)], tmp_path)
    seed = tmp_path / "seed"
    _git(["clone", str(bare), str(seed)], tmp_path)
    (seed / "README.md").write_text("hello\n")
    _git(["config", "user.email", "t@t"], seed)
    _git(["config", "user.name", "t"], seed)
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "seed"], seed)
    _git(["push", "origin", "main"], seed)
    return bare


@pytest.fixture
def wsenv(monkeypatch, tmp_path, remote):
    """Workspace configured for assignment a-123 with a stubbed vendor and the
    remote pointed at the local bare repo."""
    minted = []
    monkeypatch.setenv("WORKSPACE_TOKEN_FUNCTION", "workspace-token-vendor-test")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(ws, "_remote_url", lambda repo: str(remote))
    monkeypatch.setattr(
        ws, "_mint_token",
        lambda repo, write: minted.append({"repo": repo, "write": write}) or "tok-123",
    )
    ws.configure(assignment_id="a-123", agent_id="docwriter", origin="acme/web")
    return minted


def test_enabled_requires_vendor_and_assignment(monkeypatch):
    ws.configure(assignment_id="", agent_id="x", origin="")
    monkeypatch.delenv("WORKSPACE_TOKEN_FUNCTION", raising=False)
    assert not ws.enabled()
    monkeypatch.setenv("WORKSPACE_TOKEN_FUNCTION", "fn")
    assert not ws.enabled()  # still no assignment
    ws.configure(assignment_id="a-1", agent_id="x", origin="")
    assert ws.enabled()


def test_clone_creates_wip_branch(wsenv):
    out = ws.clone_repo("acme/web")
    assert "Cloned acme/web" in out
    repo_dir = ws._state.cloned["acme/web"]
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_dir), capture_output=True, text=True,
    ).stdout.strip()
    assert head == "wip/a-123"


def test_clone_mints_read_token_only(wsenv):
    ws.clone_repo("acme/web")
    assert all(not m["write"] for m in wsenv)


def test_second_push_in_same_session_succeeds(wsenv, remote):
    """Regression: shallow clones are single-branch (no origin/wip remote-
    tracking ref), so a bare --force-with-lease rejects EVERY push after the
    branch exists. The explicit tracked-sha lease must allow consecutive
    checkpoints."""
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "one.txt").write_text("1\n")
    assert "ERROR" not in ws.commit_and_push("acme/web", "checkpoint 1")
    (repo_dir / "two.txt").write_text("2\n")
    out = ws.commit_and_push("acme/web", "checkpoint 2")
    assert "ERROR" not in out
    remote_sha = subprocess.run(
        ["git", "rev-parse", "wip/a-123"], cwd=str(remote),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_sha == ws._head_sha(repo_dir)


def test_push_after_resume_reclone_succeeds(wsenv):
    """Regression: a re-clone (resume) has no remote-tracking ref either — the
    push after restore must succeed via the seeded lease base."""
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "pre.txt").write_text("pre\n")
    snapshot = ws.push_all_clean()
    import shutil as _shutil

    _shutil.rmtree(ws.workspace_root())
    ws.configure(assignment_id="a-123", agent_id="docwriter", origin="acme/web")
    ws.restore(snapshot)
    new_dir = ws._state.cloned["acme/web"]
    (new_dir / "post.txt").write_text("post\n")
    out = ws.commit_and_push("acme/web", "after resume")
    assert "ERROR" not in out


def test_push_lease_lost_to_foreign_push_fails_loud(wsenv, remote, tmp_path):
    """The explicit lease must retain force-with-lease's safety property: a
    push that would clobber a commit someone else landed is rejected."""
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "mine.txt").write_text("mine\n")
    ws.commit_and_push("acme/web", "mine")
    # A foreign writer advances the remote wip branch behind our back.
    foreign = tmp_path / "foreign"
    subprocess.run(["git", "clone", "-q", str(remote), str(foreign)], check=True)
    _git(["config", "user.email", "f@f"], foreign)
    _git(["config", "user.name", "f"], foreign)
    subprocess.run(
        ["git", "fetch", "-q", "origin", "wip/a-123:wip/a-123"], cwd=str(foreign), check=True
    )
    subprocess.run(["git", "checkout", "-q", "wip/a-123"], cwd=str(foreign), check=True)
    (foreign / "theirs.txt").write_text("theirs\n")
    _git(["add", "-A"], foreign)
    _git(["commit", "-qm", "theirs"], foreign)
    subprocess.run(["git", "push", "-q", "origin", "wip/a-123"], cwd=str(foreign), check=True)
    # Our next push leases against our (now stale) tracked sha and must fail.
    (repo_dir / "more.txt").write_text("more\n")
    out = ws.commit_and_push("acme/web", "more")
    assert "ERROR" in out


def test_commit_and_push_lands_and_verifies(wsenv, remote):
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "new.txt").write_text("work\n")
    out = ws.commit_and_push("acme/web", "add new.txt")
    assert "committed and pushed" in out
    # The wip branch exists on the remote at the local sha.
    remote_sha = subprocess.run(
        ["git", "rev-parse", "wip/a-123"], cwd=str(remote),
        capture_output=True, text=True,
    ).stdout.strip()
    local_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir),
        capture_output=True, text=True,
    ).stdout.strip()
    assert remote_sha == local_sha
    # Push minted a WRITE token.
    assert any(m["write"] for m in wsenv)


def test_push_verification_fails_loud(wsenv, monkeypatch):
    """D7: a push that doesn't land verifiably must raise, not pass silently."""
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "f.txt").write_text("x\n")
    ws._commit_if_dirty(repo_dir, "wip")
    monkeypatch.setattr(ws, "_remote_branch_sha", lambda repo, branch, token: "deadbeef")
    with pytest.raises(ws.WorkspaceError, match="push verification failed"):
        ws._push("acme/web", repo_dir)


def test_push_all_clean_returns_snapshot(wsenv):
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "wip.txt").write_text("in progress\n")
    snapshot = ws.push_all_clean()
    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["repo"] == "acme/web"
    assert entry["branch"] == "wip/a-123"
    assert len(entry["sha"]) == 40
    # Tree is clean after the checkpoint commit.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo_dir),
        capture_output=True, text=True,
    ).stdout
    assert status.strip() == ""


def test_restore_checks_out_recorded_sha(wsenv):
    # Simulate a pause: push work, snapshot, blow the workspace away.
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "durable.txt").write_text("must survive\n")
    snapshot = ws.push_all_clean()
    import shutil as _shutil

    _shutil.rmtree(ws.workspace_root())
    ws.configure(assignment_id="a-123", agent_id="docwriter", origin="acme/web")
    # Resume: restore from the snapshot in a fresh workspace.
    restored = ws.restore(snapshot)
    assert restored == ["acme/web"]
    new_dir = ws._state.cloned["acme/web"]
    assert (new_dir / "durable.txt").read_text() == "must survive\n"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(new_dir),
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == snapshot[0]["sha"]


def test_reclone_resumes_existing_wip_branch(wsenv):
    """A plain clone_repo after a prior push picks the wip branch back up."""
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    (repo_dir / "step1.txt").write_text("1\n")
    ws.commit_and_push("acme/web", "step 1")
    sha = ws._head_sha(repo_dir)
    import shutil as _shutil

    _shutil.rmtree(ws.workspace_root())
    ws.configure(assignment_id="a-123", agent_id="docwriter", origin="acme/web")
    ws.clone_repo("acme/web")
    new_dir = ws._state.cloned["acme/web"]
    assert ws._head_sha(new_dir) == sha
    assert (new_dir / "step1.txt").exists()


def test_token_never_written_to_git_config(wsenv):
    """The credential is per-command (-c http.extraheader) — it must not
    persist in the clone's .git/config."""
    ws.clone_repo("acme/web")
    repo_dir = ws._state.cloned["acme/web"]
    config = (repo_dir / ".git" / "config").read_text()
    assert "tok-123" not in config
    assert "extraheader" not in config.lower()


def test_workspace_run_executes_in_tree(wsenv):
    ws.clone_repo("acme/web")
    out = ws.workspace_run("cat README.md", "acme/web")
    assert "exit code 0" in out
    assert "hello" in out


def test_workspace_run_requires_clone(wsenv):
    assert "not cloned" in ws.workspace_run("ls", "acme/other")


def test_workspace_run_scrubs_cloud_credentials(wsenv, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    ws.clone_repo("acme/web")
    out = ws.workspace_run("env", "acme/web")
    assert "supersecret" not in out


def test_tool_errors_are_returned_not_raised(wsenv, monkeypatch):
    """Model-visible tools return ERROR strings the model can react to."""
    def boom(repo, write):
        raise ws.WorkspaceError("vendor said no")

    monkeypatch.setattr(ws, "_mint_token", boom)
    assert "ERROR: vendor said no" in ws.clone_repo("acme/web")

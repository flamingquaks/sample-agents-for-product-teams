"""Unit tests for the pure logic in scripts/bootstrap.py.

The AWS/sam orchestration is exercised by a real bootstrap (needs those CLIs +
credentials); these tests cover the decision logic that must be correct
regardless: tool detection + install guidance, profile parsing, config
round-trip, and the Runner's dry-run contract. (The OIDC deploy-role + per-agent
IAM role creation were removed when the fleet moved to UI-driven onboarding —
the dashboard's capability-deployer owns runtime roles now.)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bootstrap  # noqa: E402


# --- tooling preflight -------------------------------------------------------


def test_detect_tools_reports_each(monkeypatch):
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda t: None if t == "sam" else "/usr/bin/" + t
    )
    present = bootstrap.detect_tools()
    assert present == {"aws": True, "sam": False}


def test_required_tools_are_aws_sam_not_gh_or_docker():
    # Thin base deploy: needs aws + sam. gh was dropped (no CI secrets to set),
    # docker was never needed (the dashboard's CodeBuild builds agent images).
    assert bootstrap.REQUIRED_TOOLS == ("aws", "sam")
    assert "gh" not in bootstrap.REQUIRED_TOOLS
    assert "docker" not in bootstrap.REQUIRED_TOOLS


def test_install_hint_is_os_specific(monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
    assert "brew install aws-sam-cli" in bootstrap.install_hint("sam")
    for tool in bootstrap.REQUIRED_TOOLS:
        assert "docs:" in bootstrap.install_hint(tool)


def test_preflight_passes_when_all_present(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "detect_tools", lambda: dict.fromkeys(bootstrap.REQUIRED_TOOLS, True)
    )
    assert bootstrap.preflight_tools() is True


def test_preflight_fails_when_a_tool_missing(monkeypatch):
    present = dict.fromkeys(bootstrap.REQUIRED_TOOLS, True)
    present["sam"] = False
    monkeypatch.setattr(bootstrap, "detect_tools", lambda: present)
    assert bootstrap.preflight_tools() is False


# --- profile parsing ---------------------------------------------------------


def test_list_profiles_parses_config_and_credentials(monkeypatch, tmp_path):
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[default]\n\n[profile prod]\n")
    (aws / "credentials").write_text("[default]\n\n[sandbox]\n")
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    profiles = bootstrap.list_profiles()
    assert {"default", "prod", "sandbox"} <= set(profiles)
    assert profiles.count("default") == 1


# --- config persistence ------------------------------------------------------


def test_config_round_trip(monkeypatch, tmp_path):
    path = tmp_path / "bootstrap.config.json"
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", path)
    bootstrap.save_config({"stage": "dev", "agents": ["adr"]})
    assert bootstrap.load_config() == {"stage": "dev", "agents": ["adr"]}


def test_load_config_corrupt_is_empty(monkeypatch, tmp_path):
    path = tmp_path / "bootstrap.config.json"
    path.write_text("{not json")
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", path)
    assert bootstrap.load_config() == {}


# --- Runner dry-run contract -------------------------------------------------


def test_runner_dry_run_does_not_execute(monkeypatch):
    called = {"run": False}
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *a, **k: called.__setitem__("run", True)
    )
    r = bootstrap.Runner(dry_run=True, profile="p", region="us-west-2")
    assert r.run(["sam", "deploy"]) is None
    assert called["run"] is False


def test_runner_aws_base_includes_profile_and_region():
    r = bootstrap.Runner(dry_run=True, profile="prod", region="eu-west-1")
    assert r._aws_base() == ["aws", "--profile", "prod", "--region", "eu-west-1"]
    assert (
        "--profile"
        not in bootstrap.Runner(
            dry_run=True, profile=None, region="us-west-2"
        )._aws_base()
    )


# --- failure tracking (#1) ---------------------------------------------------


class _FakeResult:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_step_records_failure_and_returns_false(monkeypatch):
    r = bootstrap.Runner(dry_run=False, profile=None, region="us-west-2")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: _FakeResult(254, stderr="An error occurred: AccessDenied"),
    )
    ok = r.step("create role", ["aws", "iam", "create-role"])
    assert ok is False
    assert len(r.failures) == 1
    assert "create role" in r.failures[0] and "AccessDenied" in r.failures[0]


def test_step_success_records_nothing(monkeypatch):
    r = bootstrap.Runner(dry_run=False, profile=None, region="us-west-2")
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *a, **k: _FakeResult(0, stdout="{}")
    )
    assert r.step("x", ["aws", "iam", "create-role"]) is True
    assert r.failures == []


def test_step_dry_run_is_true_and_silent():
    r = bootstrap.Runner(dry_run=True, profile=None, region="us-west-2")
    assert r.step("x", ["aws", "iam", "create-role"]) is True
    assert r.failures == []


# --- aws_exists trichotomy (#4) ----------------------------------------------


def test_aws_exists_present_absent_ambiguous(monkeypatch):
    r = bootstrap.Runner(dry_run=False, profile=None, region="us-west-2")

    def result_for(rc, err=""):
        return lambda *a, **k: _FakeResult(
            rc, stdout="{}" if rc == 0 else "", stderr=err
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", result_for(0))
    assert r.aws_exists(["iam", "get-role", "--role-name", "x"]) is True

    monkeypatch.setattr(
        bootstrap.subprocess, "run", result_for(254, "NoSuchEntity: not found")
    )
    assert r.aws_exists(["iam", "get-role", "--role-name", "x"]) is False

    # Transient/permission error → ambiguous (None), NOT "absent".
    monkeypatch.setattr(
        bootstrap.subprocess, "run", result_for(254, "Throttling: rate exceeded")
    )
    assert r.aws_exists(["iam", "get-role", "--role-name", "x"]) is None


def test_aws_exists_dry_run_is_none():
    r = bootstrap.Runner(dry_run=True, profile=None, region="us-west-2")
    assert r.aws_exists(["iam", "get-role", "--role-name", "x"]) is None

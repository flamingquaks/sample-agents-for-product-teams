"""Unit tests for the pure logic in scripts/deploy.py.

The AWS/sam/docker orchestration is exercised by a real deploy (needs those
CLIs + credentials); these tests cover the decision logic that must be correct
regardless: tool detection + install guidance, profile parsing, per-agent IAM
policy / env generation, config round-trip, and the Runner's dry-run contract.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy  # noqa: E402


# --- tooling preflight -------------------------------------------------------


def test_detect_tools_reports_each(monkeypatch):
    monkeypatch.setattr(
        deploy.shutil, "which", lambda t: "/usr/bin/" + t if t != "sam" else None
    )
    present = deploy.detect_tools()
    assert present["aws"] is True
    assert present["docker"] is True
    assert present["sam"] is False


def test_install_hint_is_os_specific(monkeypatch):
    monkeypatch.setattr(deploy.platform, "system", lambda: "Darwin")
    assert "brew install aws-sam-cli" in deploy.install_hint("sam")
    monkeypatch.setattr(deploy.platform, "system", lambda: "Linux")
    assert "get.docker.com" in deploy.install_hint("docker")
    # every tool has a docs link
    for tool in deploy.REQUIRED_TOOLS:
        assert "docs:" in deploy.install_hint(tool)


def test_preflight_fails_when_a_tool_missing(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy, "detect_tools", lambda: {"aws": True, "sam": False, "docker": True}
    )
    monkeypatch.setattr(deploy, "docker_daemon_ok", lambda: True)
    assert deploy.preflight_tools() is False
    assert "❌ sam" in capsys.readouterr().out


def test_preflight_fails_when_docker_daemon_down(monkeypatch):
    monkeypatch.setattr(
        deploy, "detect_tools", lambda: {"aws": True, "sam": True, "docker": True}
    )
    monkeypatch.setattr(deploy, "docker_daemon_ok", lambda: False)
    assert deploy.preflight_tools() is False


def test_preflight_passes_when_all_present(monkeypatch):
    monkeypatch.setattr(
        deploy, "detect_tools", lambda: dict.fromkeys(deploy.REQUIRED_TOOLS, True)
    )
    monkeypatch.setattr(deploy, "docker_daemon_ok", lambda: True)
    assert deploy.preflight_tools() is True


# --- profile parsing ---------------------------------------------------------


def test_list_profiles_parses_config_and_credentials(monkeypatch, tmp_path):
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text(
        "[default]\nregion = us-west-2\n\n[profile prod]\nregion = us-east-1\n"
    )
    (aws / "credentials").write_text("[default]\n\n[sandbox]\n")
    monkeypatch.setattr(deploy.Path, "home", classmethod(lambda cls: tmp_path))
    profiles = deploy.list_profiles()
    assert "default" in profiles
    assert "prod" in profiles  # "profile " prefix stripped
    assert "sandbox" in profiles  # from credentials
    assert profiles.count("default") == 1  # de-duped across both files


# --- per-agent IAM + env -----------------------------------------------------


def test_agent_role_policies_baseline_and_ssm():
    pols = deploy.agent_role_policies("workitems", "us-west-2", "123456789012", "dev")
    assert {"cloudwatch-logs", "dynamodb-assignments", "ecr-pull", "ssm-read"} <= set(
        pols
    )
    # assignments table is stage-scoped
    ddb = json.dumps(pols["dynamodb-assignments"])
    assert "table/dispatch-assignments-dev" in ddb
    # workitems reads asana + github MCP creds
    ssm = json.dumps(pols["ssm-read"])
    assert "asana-mcp-*" in ssm and "github-mcp-*" in ssm and "asana-pat" in ssm


def test_adr_role_has_no_asana_ssm():
    pols = deploy.agent_role_policies("adr", "us-west-2", "123456789012", "dev")
    ssm = json.dumps(pols["ssm-read"])
    assert "github-mcp-*" in ssm
    assert "asana" not in ssm  # adr is GitHub-only


def test_agent_env_per_agent_shape():
    cfg = {
        "target_repo": "acme/web",
        "asana_project_gid": "111",
        "asana_workspace_gid": "222",
    }
    wi = deploy.agent_env("workitems", cfg, "dev", "gr-1", "DRAFT")
    assert wi["GITHUB_REPO"] == "acme/web"
    assert wi["ASANA_PROJECT_GID"] == "111"
    assert wi["ASSIGNMENTS_TABLE"] == "dispatch-assignments-dev"
    assert wi["BEDROCK_GUARDRAIL_ID"] == "gr-1"

    # researcher has no GITHUB_REPO; adr has no Asana vars
    assert "GITHUB_REPO" not in deploy.agent_env("researcher", cfg, "dev", "g", "DRAFT")
    adr = deploy.agent_env("adr", cfg, "dev", "g", "DRAFT")
    assert "ASANA_PROJECT_GID" not in adr and adr["GITHUB_REPO"] == "acme/web"


def test_agent_env_omits_empty_values():
    # No repo/asana configured → those keys are dropped, not set to "".
    env = deploy.agent_env("workitems", {}, "dev", "g", "DRAFT")
    assert "GITHUB_REPO" not in env
    assert "ASANA_PROJECT_GID" not in env
    assert env["ASSIGNMENTS_TABLE"] == "dispatch-assignments-dev"


def test_agent_env_stage_scopes_table():
    env = deploy.agent_env("adr", {"target_repo": "a/b"}, "prod", "g", "1")
    assert env["ASSIGNMENTS_TABLE"] == "dispatch-assignments-prod"


# --- config persistence ------------------------------------------------------


def test_config_round_trip(monkeypatch, tmp_path):
    path = tmp_path / "deploy.config.json"
    monkeypatch.setattr(deploy, "CONFIG_PATH", path)
    deploy.save_config({"stage": "dev", "agents": ["adr"]})
    assert deploy.load_config() == {"stage": "dev", "agents": ["adr"]}


def test_load_config_missing_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "CONFIG_PATH", tmp_path / "nope.json")
    assert deploy.load_config() == {}


def test_load_config_corrupt_is_empty(monkeypatch, tmp_path):
    path = tmp_path / "deploy.config.json"
    path.write_text("{not json")
    monkeypatch.setattr(deploy, "CONFIG_PATH", path)
    assert deploy.load_config() == {}


# --- Runner dry-run contract -------------------------------------------------


def test_runner_dry_run_does_not_execute(monkeypatch):
    called = {"run": False}
    monkeypatch.setattr(
        deploy.subprocess, "run", lambda *a, **k: called.__setitem__("run", True)
    )
    r = deploy.Runner(dry_run=True, profile="p", region="us-west-2")
    assert r.run(["docker", "build", "."]) is None
    assert called["run"] is False  # nothing executed under dry-run


def test_runner_aws_base_includes_profile_and_region():
    r = deploy.Runner(dry_run=True, profile="prod", region="eu-west-1")
    base = r._aws_base()
    assert base == ["aws", "--profile", "prod", "--region", "eu-west-1"]
    # no profile → omitted
    r2 = deploy.Runner(dry_run=True, profile=None, region="us-west-2")
    assert "--profile" not in r2._aws_base()

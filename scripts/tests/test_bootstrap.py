"""Unit tests for the pure logic in scripts/bootstrap.py.

The AWS/sam/gh orchestration is exercised by a real bootstrap (needs those CLIs
+ credentials); these tests cover the decision logic that must be correct
regardless: tool detection + install guidance, profile parsing, the OIDC
deploy-role trust policy, per-agent IAM policy generation, config round-trip,
and the Runner's dry-run contract.
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
    assert present == {"aws": True, "sam": False, "gh": True}


def test_required_tools_are_aws_sam_gh_not_docker():
    # Bootstrap doesn't build images — docker is CI's concern, not this script's.
    assert bootstrap.REQUIRED_TOOLS == ("aws", "sam", "gh")
    assert "docker" not in bootstrap.REQUIRED_TOOLS


def test_install_hint_is_os_specific(monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
    assert "brew install aws-sam-cli" in bootstrap.install_hint("sam")
    assert "brew install gh" in bootstrap.install_hint("gh")
    for tool in bootstrap.REQUIRED_TOOLS:
        assert "docs:" in bootstrap.install_hint(tool)


def test_preflight_fails_when_gh_unauthenticated(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "detect_tools", lambda: dict.fromkeys(bootstrap.REQUIRED_TOOLS, True)
    )
    monkeypatch.setattr(bootstrap, "gh_authenticated", lambda: False)
    assert bootstrap.preflight_tools() is False


def test_preflight_passes_when_all_present_and_gh_authed(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "detect_tools", lambda: dict.fromkeys(bootstrap.REQUIRED_TOOLS, True)
    )
    monkeypatch.setattr(bootstrap, "gh_authenticated", lambda: True)
    assert bootstrap.preflight_tools() is True


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


# --- OIDC deploy-role trust --------------------------------------------------


def test_deploy_role_trust_is_repo_scoped_string_equals():
    trust = bootstrap.deploy_role_trust("123456789012", "acme", "web")
    cond = trust["Statement"][0]["Condition"]
    # Must be StringEquals (not a StringLike wildcard) restricted to main + PRs.
    assert "StringLike" not in cond
    subs = cond["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert subs == [
        "repo:acme/web:ref:refs/heads/main",
        "repo:acme/web:pull_request",
    ]
    assert (
        cond["StringEquals"]["token.actions.githubusercontent.com:aud"]
        == "sts.amazonaws.com"
    )
    principal = trust["Statement"][0]["Principal"]["Federated"]
    assert principal.endswith(":oidc-provider/token.actions.githubusercontent.com")
    assert "123456789012" in principal


# --- per-agent IAM -----------------------------------------------------------


def test_agent_role_policies_baseline_and_ssm():
    pols = bootstrap.agent_role_policies(
        "workitems", "us-west-2", "123456789012", "dev"
    )
    assert {"cloudwatch-logs", "dynamodb-assignments", "ecr-pull", "ssm-read"} <= set(
        pols
    )
    assert "table/dispatch-assignments-dev" in json.dumps(pols["dynamodb-assignments"])
    ssm = json.dumps(pols["ssm-read"])
    assert "asana-mcp-*" in ssm and "github-mcp-*" in ssm and "asana-pat" in ssm


def test_adr_role_is_github_only():
    ssm = json.dumps(
        bootstrap.agent_role_policies("adr", "us-west-2", "123456789012", "dev")[
            "ssm-read"
        ]
    )
    assert "github-mcp-*" in ssm
    assert "asana" not in ssm


def test_agent_role_policies_stage_scopes_table():
    pols = bootstrap.agent_role_policies("adr", "us-west-2", "123456789012", "prod")
    assert "table/dispatch-assignments-prod" in json.dumps(pols["dynamodb-assignments"])


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


# --- scoped deploy-role policy (#3) ------------------------------------------


def test_deploy_role_policy_is_scoped_not_admin():
    import json as _json

    pol = bootstrap.deploy_role_policy("us-west-2", "123456789012")
    blob = _json.dumps(pol)
    # No AdministratorAccess / blanket allow-all.
    assert "AdministratorAccess" not in blob
    assert not any(s.get("Action") == "*" for s in pol["Statement"])
    sids = {s.get("Sid") for s in pol["Statement"]}
    assert {"EcrPushPull", "AgentCoreRuntime", "PassAgentRuntimeRoles"} <= sids
    # The @mention dispatch path (agent-dispatch.yml) invokes the router Lambda —
    # the scoped role must grant it (regression: it was missing, so every
    # dispatch would have 403'd under the scoped role).
    invoke = next(s for s in pol["Statement"] if s["Sid"] == "InvokeDispatchRouter")
    assert invoke["Action"] == "lambda:InvokeFunction"
    assert ":function:dispatch-router-*" in invoke["Resource"]
    # PassRole is limited to the per-agent runtime roles + the agentcore service.
    passrole = next(s for s in pol["Statement"] if s["Sid"] == "PassAgentRuntimeRoles")
    assert passrole["Resource"].endswith(":role/*-agentcore-runtime")
    assert (
        passrole["Condition"]["StringEquals"]["iam:PassedToService"]
        == "bedrock-agentcore.amazonaws.com"
    )
    # No cloudformation *write* — the foundation sam deploy runs locally, not in CI.
    assert "cloudformation:CreateStack" not in blob and "cloudformation:*" not in blob


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

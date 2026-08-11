"""Unit tests for the Jira broker Lambda target (jira_broker.py).

The broker dispatches on the gateway's ``bedrockAgentCoreToolName``, enforces the
onboarded-project allowlist + issue-key↔project-key arg consistency, and calls
Jira REST v3 with the site service-account token. The token fetch
(atlassian_client) and the HTTP layer (requests) are patched, so these exercise
routing, the container allowlist, arg-consistency, and the curated/destructive
tool boundary without AWS or network.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jira_broker  # noqa: E402
import atlassian_client as ac  # noqa: E402


SITE_ID = "11111111-1111-1111-1111-111111111111"


def _ctx(tool_name: str):
    ctx = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": f"JiraTarget___{tool_name}"}
    return ctx


def _args(agent="workitems", origin="acme/web", **extra):
    base = {jira_broker.ORIGIN_ARG: origin, jira_broker.AGENT_ARG: agent, "site": SITE_ID}
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _wiring(monkeypatch):
    """Site exists + jira-enabled; token fetch stubbed; ENG project allowed."""
    site = {"site_id": SITE_ID, "site_url": "https://acme.atlassian.net",
            "bot_email": "sdlc@acme.com", "api_token_param": "/p"}
    monkeypatch.setattr(ac, "get_site", lambda sid: site if sid == SITE_ID else None)
    monkeypatch.setattr(ac, "fetch_token", lambda s: "tok")
    monkeypatch.setattr(
        jira_broker.trigger_grants, "atlassian_product_enabled",
        lambda sid, product: sid == SITE_ID and product == "jira",
    )
    monkeypatch.setattr(
        jira_broker.trigger_grants, "container_allowed",
        lambda sid, key, product: key == "ENG" and product == "jira",
    )
    return site


def _capture(monkeypatch, status=200, payload=None):
    calls = []

    def fake_rest(site, method, path, token, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return status, (payload if payload is not None else {})

    monkeypatch.setattr(ac, "rest", fake_rest)
    return calls


def test_get_issue_reads_allowed_project(monkeypatch):
    calls = _capture(monkeypatch, payload={"key": "ENG-1", "fields": {"summary": "hi"}})
    out = jira_broker.handler(_args(issue_key="ENG-1"), _ctx("get_issue"))
    assert out["issue_key"] == "ENG-1"
    assert calls[0]["path"] == "/rest/api/3/issue/ENG-1"


def test_write_to_unonboarded_project_refused(monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(jira_broker.BrokerError, match="not onboarded"):
        jira_broker.handler(
            _args(issue_key="OPS-9", project_key="OPS", body="x"), _ctx("add_comment")
        )


def test_project_key_mismatch_refused(monkeypatch):
    _capture(monkeypatch)
    # issue is ENG-1 (project ENG) but caller claims project_key OPS → refused.
    with pytest.raises(jira_broker.BrokerError, match="does not match"):
        jira_broker.handler(
            _args(issue_key="ENG-1", project_key="OPS", body="x"), _ctx("add_comment")
        )


def test_add_comment_wraps_adf_and_signs(monkeypatch):
    calls = _capture(monkeypatch, payload={"id": "10"})
    out = jira_broker.handler(
        _args(issue_key="ENG-1", project_key="ENG", body="looks good"),
        _ctx("add_comment"),
    )
    assert out["comment_id"] == "10"
    body = calls[0]["body"]["body"]
    assert body["type"] == "doc"  # ADF-wrapped
    flat = ac.adf_to_text(body)
    assert "looks good" in flat and "[workitems]" in flat  # signature line


def test_unknown_tool_raises(monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(jira_broker.BrokerError, match="unknown tool"):
        jira_broker.handler(_args(issue_key="ENG-1"), _ctx("delete_issue"))


def test_site_not_enabled_refused(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(
        jira_broker.trigger_grants, "atlassian_product_enabled", lambda sid, p: False
    )
    with pytest.raises(jira_broker.BrokerError, match="not an onboarded"):
        jira_broker.handler(_args(issue_key="ENG-1"), _ctx("get_issue"))


def test_transition_resolves_name_to_id(monkeypatch):
    seq = [
        (200, {"transitions": [{"id": "31", "name": "Done"}]}),
        (204, {}),
    ]
    calls = []

    def fake_rest(site, method, path, token, *, params=None, body=None):
        calls.append((method, path, body))
        return seq.pop(0)

    monkeypatch.setattr(ac, "rest", fake_rest)
    out = jira_broker.handler(
        _args(issue_key="ENG-1", project_key="ENG", transition_name="Done"),
        _ctx("transition_issue"),
    )
    assert out["transitioned_to"] == "Done"
    # Second call posts the resolved transition id.
    assert calls[1][2]["transition"]["id"] == "31"


def test_search_pins_jql_to_onboarded_projects(monkeypatch):
    # search_issues AND-s the model's JQL under a project-allowlist pin (§B2.4).
    calls = _capture(monkeypatch, payload={"issues": []})
    monkeypatch.setattr(jira_broker, "_allowed_project_keys", lambda sid: {"ENG"})
    jira_broker.handler(_args(jql="text ~ 'x'"), _ctx("search_issues"))
    sent_jql = calls[0]["params"]["jql"]
    assert 'project in ("ENG")' in sent_jql
    assert "(text ~ 'x')" in sent_jql  # model JQL kept, but AND-ed under the pin


def test_search_post_filters_jql_injection_breakout(monkeypatch):
    # THE security boundary: even if a crafted model JQL breaks out of the pin
    # via operator precedence (`…) OR (project = SECRET`) and Jira returns issues
    # from a NON-onboarded project, the broker drops them by project prefix — the
    # injection-proof post-filter. ENG is onboarded; OPS is not.
    _capture(monkeypatch, payload={"issues": [
        {"key": "ENG-1", "fields": {"summary": "ok", "status": {"name": "Open"}}},
        {"key": "OPS-9", "fields": {"summary": "leak", "status": {"name": "Open"}}},
    ]})
    monkeypatch.setattr(jira_broker, "_allowed_project_keys", lambda sid: {"ENG"})
    out = jira_broker.handler(
        _args(jql="x = x) OR (project = OPS"), _ctx("search_issues")
    )
    keys = {i["issue_key"] for i in out["issues"]}
    assert keys == {"ENG-1"}  # OPS-9 dropped despite the JQL breakout


def test_search_refused_when_no_project_onboarded(monkeypatch):
    _capture(monkeypatch, payload={"issues": []})
    monkeypatch.setattr(jira_broker, "_allowed_project_keys", lambda sid: set())
    out = jira_broker.handler(_args(jql="order by created"), _ctx("search_issues"))
    assert out.get("isError") and "no Jira projects" in out["message"]


def test_list_projects_filtered_to_onboarded(monkeypatch):
    calls = _capture(monkeypatch, payload={"values": [
        {"key": "ENG", "name": "Eng"}, {"key": "OPS", "name": "Ops"}]})
    monkeypatch.setattr(jira_broker, "_allowed_project_keys", lambda sid: {"ENG"})
    out = jira_broker.handler(_args(), _ctx("list_projects"))
    keys = {p["project_key"] for p in out["projects"]}
    assert keys == {"ENG"}  # OPS is not onboarded → filtered out


def test_no_delete_tools_in_schema():
    names = {t["name"] for t in jira_broker.tool_definitions()}
    assert not any("delete" in n or "worklog" in n or "sprint" in n for n in names)
    # The curated write set is exactly the spec's §B2.2 table.
    assert {"add_comment", "create_issue", "update_issue", "transition_issue",
            "assign_issue", "link_issues", "add_remote_link"} <= names

"""Tests for the dashboard API Lambda handler (routing + auth gate)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402


OPERATOR_CLAIMS = {"sub": "op-1", "cognito:groups": "operators"}


def _event(resource, claims=OPERATOR_CLAIMS, path=None, qs=None):
    return {
        "resource": resource,
        "pathParameters": path,
        "queryStringParameters": qs,
        "requestContext": {"authorizer": {"claims": claims}},
    }


@pytest.fixture(autouse=True)
def stub_queries(monkeypatch):
    """Replace the query layer so handler tests don't touch DynamoDB."""
    monkeypatch.setattr(
        api.queries, "list_runs", lambda params: {"runs": [], "next_token": None}
    )
    monkeypatch.setattr(
        api.queries,
        "get_run",
        lambda aid: {"assignment_id": aid} if aid == "a-1" else None,
    )
    monkeypatch.setattr(
        api.queries,
        "trace",
        lambda dim, val: {"dimension": dim, "value": val, "runs": []},
    )
    monkeypatch.setattr(api.queries, "stats", lambda: {"total": 0})
    # keep the real TRACE_DIMENSIONS set


def _body(resp):
    return json.loads(resp["body"])


def test_non_operator_gets_403():
    resp = api.handler(
        _event("/runs", claims={"sub": "u", "cognito:groups": "viewers"})
    )
    assert resp["statusCode"] == 403


def test_unauthenticated_gets_403():
    resp = api.handler({"resource": "/runs", "requestContext": {}})
    assert resp["statusCode"] == 403


def test_list_runs_ok():
    resp = api.handler(_event("/runs"))
    assert resp["statusCode"] == 200
    assert _body(resp) == {"runs": [], "next_token": None}


def test_get_run_found():
    resp = api.handler(_event("/runs/{assignment_id}", path={"assignment_id": "a-1"}))
    assert resp["statusCode"] == 200
    assert _body(resp)["assignment_id"] == "a-1"


def test_get_run_not_found():
    resp = api.handler(_event("/runs/{assignment_id}", path={"assignment_id": "nope"}))
    assert resp["statusCode"] == 404


def test_trace_requires_params():
    resp = api.handler(_event("/trace", qs={"dim": "repo"}))
    assert resp["statusCode"] == 400


def test_trace_rejects_unknown_dimension():
    resp = api.handler(_event("/trace", qs={"dim": "bogus", "value": "x"}))
    assert resp["statusCode"] == 400


def test_trace_ok():
    resp = api.handler(_event("/trace", qs={"dim": "jira_key", "value": "ABC-1"}))
    assert resp["statusCode"] == 200
    assert _body(resp)["dimension"] == "jira_key"


def test_stats_ok():
    resp = api.handler(_event("/stats"))
    assert resp["statusCode"] == 200


def test_unknown_route_404():
    resp = api.handler(_event("/nope"))
    assert resp["statusCode"] == 404


def test_internal_error_becomes_500(monkeypatch):
    def boom(params):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(api.queries, "list_runs", boom)
    resp = api.handler(_event("/runs"))
    assert resp["statusCode"] == 500

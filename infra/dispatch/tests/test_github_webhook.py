"""Tests for the GitHub App webhook receiver (github_webhook.py).

Covers the security contract (signature verification, fail-closed on missing
secret) and the dispatch behavior (agent resolution, event routing, payload
shape) with the SSM/Lambda/GitHub clients stubbed.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("GITHUB_WEBHOOK_SECRET_PARAM", "/sdlc-agents/test/github-webhook-secret")
os.environ.setdefault("DISPATCH_FUNCTION", "dispatch-router-test")

SECRET = "s3cr3t-webhook-key"


def _fresh(monkeypatch, secret=SECRET):
    """Import github_webhook with its SSM + Lambda clients stubbed."""
    sys.modules.pop("github_webhook", None)
    import github_webhook as gw

    dispatched = []

    class _ParamNotFound(Exception):
        pass

    class _Exceptions:
        ParameterNotFound = _ParamNotFound

    class _SSM:
        exceptions = _Exceptions()

        def get_parameter(self, Name, WithDecryption=False):
            if secret is None:
                raise _ParamNotFound()
            return {"Parameter": {"Value": secret}}

    class _Lambda:
        def invoke(self, FunctionName, InvocationType, Payload):
            dispatched.append(json.loads(Payload))
            return {"StatusCode": 202}

    # The receiver catches _ssm.exceptions.ParameterNotFound; the fake resolves
    # that to _ParamNotFound so the catch works without real botocore.
    monkeypatch.setattr(gw, "_ssm", _SSM())
    monkeypatch.setattr(gw, "_lambda", _Lambda())
    # Enrichment does network I/O — stub it to a fixed context.
    monkeypatch.setattr(gw, "_issue_context", lambda repo, num: {"repo": repo, "issue_number": str(num)})
    return gw, dispatched


def _sign(body: str, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _event(body: str, *, event="issue_comment", signature=None):
    return {
        "headers": {
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature if signature is not None else _sign(body),
        },
        "body": body,
    }


def _comment_body(comment_text, repo="acme/web", number=42, action="created"):
    return json.dumps(
        {
            "action": action,
            "comment": {"body": comment_text, "user": {"login": "alice"}},
            "issue": {"number": number},
            "repository": {"full_name": repo},
        }
    )


def test_missing_secret_fails_closed(monkeypatch):
    gw, dispatched = _fresh(monkeypatch, secret=None)
    body = _comment_body("@workitems do the thing")
    resp = gw.handler(_event(body))
    assert resp["statusCode"] == 503
    assert not dispatched


def test_invalid_signature_rejected(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    body = _comment_body("@workitems go")
    resp = gw.handler(_event(body, signature="sha256=deadbeef"))
    assert resp["statusCode"] == 401
    assert not dispatched


def test_valid_mention_dispatches_with_correct_payload(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    body = _comment_body("@workitems break this down", repo="acme/web", number=7)
    resp = gw.handler(_event(body))
    assert resp["statusCode"] == 200
    assert len(dispatched) == 1
    p = dispatched[0]
    assert p["source"] == "github"
    assert p["agent_id"] == "workitems"
    assert p["trigger_type"] == "comment_mention"
    assert p["sender"] == "alice"
    assert p["instruction"] == "@workitems break this down"
    assert p["context"]["repo"] == "acme/web"


def test_alias_resolves_to_canonical_agent(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    gw.handler(_event(_comment_body("@docs update the readme")))
    assert dispatched[0]["agent_id"] == "docwriter"  # docs -> docwriter


def test_no_mention_is_noop_200(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    resp = gw.handler(_event(_comment_body("just a normal comment, no agent")))
    assert resp["statusCode"] == 200
    assert not dispatched


def test_non_created_action_ignored(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    body = _comment_body("@workitems go", action="edited")
    resp = gw.handler(_event(body))
    assert resp["statusCode"] == 200
    assert not dispatched


def test_ping_event_acked(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    body = json.dumps({"zen": "hi"})
    resp = gw.handler(_event(body, event="ping"))
    assert resp["statusCode"] == 200
    assert not dispatched


def test_unrouted_event_ignored(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    body = json.dumps({"ref": "refs/heads/main"})
    resp = gw.handler(_event(body, event="push"))
    assert resp["statusCode"] == 200
    assert not dispatched


def test_pr_review_comment_routes(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    body = json.dumps(
        {
            "action": "created",
            "comment": {"body": "@adr check this", "user": {"login": "bob"}},
            "pull_request": {"number": 9},
            "repository": {"full_name": "acme/api"},
        }
    )
    resp = gw.handler(_event(body, event="pull_request_review_comment"))
    assert resp["statusCode"] == 200
    assert dispatched[0]["agent_id"] == "adr"
    assert dispatched[0]["trigger_type"] == "pr_comment"

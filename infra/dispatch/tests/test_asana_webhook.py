"""Tests for the Asana webhook receiver (asana_webhook.py).

Focus on the parts this refactor changed: registry-backed @mention resolution on
the comment path (parity with the GitHub receiver) and shared HMAC verification.
The Asana API, SSM, and Lambda-invoke clients are stubbed.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ASANA_WEBHOOK_SECRET_PARAM", "/sdlc-agents/asana-webhook-secret")
os.environ.setdefault("ASANA_PAT_PARAM", "/sdlc-agents/asana-pat")
os.environ.setdefault("REGISTRY_PARAM", "/sdlc-agents/test/registry")
os.environ.setdefault("DISPATCH_FUNCTION", "dispatch-router-test")

SECRET = "asana-webhook-secret"

# A registry containing an agent that is NOT in any legacy hardcoded roster,
# to prove comment resolution is registry-driven.
REGISTRY = {
    "agents": {
        "workitems": {"aliases": ["pm", "status", "plan"]},
        "docwriter": {"aliases": ["docs", "doc", "writer"]},
        "securityscan": {"aliases": ["sec"]},
    }
}


def _fresh(monkeypatch, *, secret=SECRET, registry=REGISTRY, story_text="", story_author="user-1"):
    """Import asana_webhook with SSM / Lambda / Asana-API access stubbed."""
    sys.modules.pop("asana_webhook", None)
    import asana_webhook as aw

    dispatched = []

    class _ParamNotFound(Exception):
        pass

    class _Exceptions:
        ParameterNotFound = _ParamNotFound
        ClientError = Exception

    class _SSM:
        exceptions = _Exceptions()

        def get_parameter(self, Name, WithDecryption=False):
            if Name == aw.REGISTRY_PARAM:
                return {"Parameter": {"Value": json.dumps(registry)}}
            if Name == aw.ASANA_WEBHOOK_SECRET_PARAM:
                if secret is None:
                    raise _ParamNotFound()
                return {"Parameter": {"Value": secret}}
            if Name == aw.ASANA_PAT_PARAM:
                return {"Parameter": {"Value": "pat-token"}}
            raise _ParamNotFound()

    class _Lambda:
        def invoke(self, FunctionName, InvocationType, Payload):
            dispatched.append(json.loads(Payload))
            return {"StatusCode": 202}

    monkeypatch.setattr(aw, "_ssm", _SSM())
    monkeypatch.setattr(aw, "lambda_client", _Lambda())
    # Stub the Asana API reads: a story (comment) and its parent task.
    monkeypatch.setattr(
        aw, "get_story",
        lambda gid, st: {"text": story_text, "created_by": {"gid": story_author}},
    )
    monkeypatch.setattr(
        aw, "get_task",
        lambda gid, st: {"gid": gid, "name": "Some task", "notes": "", "projects": []},
    )
    return aw, dispatched


def _sign(body, secret=SECRET):
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _comment_event(story_gid="s1", task_gid="t1"):
    return {
        "events": [
            {
                "resource": {"gid": story_gid, "resource_type": "story"},
                "parent": {"gid": task_gid},
                "action": "added",
            }
        ]
    }


def _invoke(aw, payload_obj, *, signature=None):
    body = json.dumps(payload_obj)
    headers = {"X-Hook-Signature": signature if signature is not None else _sign(body)}
    return aw.handler({"headers": headers, "body": body}, None)


def test_invalid_signature_rejected(monkeypatch):
    aw, dispatched = _fresh(monkeypatch, story_text="@workitems go")
    resp = _invoke(aw, _comment_event(), signature="deadbeef")
    assert resp["statusCode"] == 401
    assert not dispatched


def test_missing_secret_fails_closed(monkeypatch):
    aw, dispatched = _fresh(monkeypatch, secret=None, story_text="@workitems go")
    resp = _invoke(aw, _comment_event())
    assert resp["statusCode"] == 503
    assert not dispatched


def test_comment_mention_dispatches_via_registry(monkeypatch):
    aw, dispatched = _fresh(monkeypatch, story_text="@workitems break this down")
    resp = _invoke(aw, _comment_event())
    assert resp["statusCode"] == 200
    assert len(dispatched) == 1
    p = dispatched[0]
    assert p["source"] == "asana"
    assert p["agent_id"] == "workitems"
    assert p["trigger_type"] == "comment_mention"
    assert p["sender"] == "user-1"
    assert p["instruction"] == "break this down"


def test_comment_alias_resolves(monkeypatch):
    aw, dispatched = _fresh(monkeypatch, story_text="@docs refresh the guide")
    _invoke(aw, _comment_event())
    assert dispatched[0]["agent_id"] == "docwriter"


def test_ui_onboarded_agent_resolves_from_asana(monkeypatch):
    # 'securityscan' is only in the registry, not in any legacy roster — it must
    # still resolve, proving Asana comment resolution is registry-driven.
    aw, dispatched = _fresh(monkeypatch, story_text="@securityscan please review")
    _invoke(aw, _comment_event())
    assert dispatched and dispatched[0]["agent_id"] == "securityscan"
    aw2, dispatched2 = _fresh(monkeypatch, story_text="@sec please review")
    _invoke(aw2, _comment_event())
    assert dispatched2 and dispatched2[0]["agent_id"] == "securityscan"


def test_no_known_mention_is_noop(monkeypatch):
    aw, dispatched = _fresh(monkeypatch, story_text="@ghost do a thing")
    resp = _invoke(aw, _comment_event())
    assert resp["statusCode"] == 200
    assert not dispatched


def test_base64_encoded_body_verifies_and_dispatches(monkeypatch):
    import base64

    aw, dispatched = _fresh(monkeypatch, story_text="@workitems break this down")
    body = json.dumps(_comment_event())
    event = {
        "headers": {"X-Hook-Signature": _sign(body)},  # signed over decoded bytes
        "body": base64.b64encode(body.encode()).decode(),
        "isBase64Encoded": True,
    }
    resp = aw.handler(event, None)
    assert resp["statusCode"] == 200
    assert dispatched and dispatched[0]["agent_id"] == "workitems"


def test_handshake_echoes_hook_secret(monkeypatch):
    aw, dispatched = _fresh(monkeypatch)
    captured = {}

    class _SSMPut:
        class exceptions:
            ClientError = Exception

        def put_parameter(self, Name, Value, Type, Overwrite):
            captured["value"] = Value

    monkeypatch.setattr(aw, "_ssm", _SSMPut())
    resp = aw.handler({"headers": {"X-Hook-Secret": "hs-123"}, "body": ""}, None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["X-Hook-Secret"] == "hs-123"
    assert captured["value"] == "hs-123"
    assert not dispatched


def test_unset_bot_gids_dropped_from_bot_users(monkeypatch):
    """With no bot GIDs configured (the default now that the template defaults
    them to ""), BOT_USERS must be EMPTY — never a {"" : "workitems"} entry that
    an unassigned task (assignee_gid == "") would match and wrongly dispatch."""
    for var in ("WORKITEMS_BOT_GID", "UAT_BOT_GID", "RESEARCHER_BOT_GID", "DOCWRITER_BOT_GID"):
        monkeypatch.delenv(var, raising=False)
    sys.modules.pop("asana_webhook", None)
    import asana_webhook as aw
    assert aw.BOT_USERS == {}
    assert "" not in aw.BOT_USERS
    # An unassigned task resolves to no agent.
    assert aw.BOT_USERS.get("") is None


def test_configured_bot_gid_maps_to_agent(monkeypatch):
    monkeypatch.setenv("WORKITEMS_BOT_GID", "1201234567890")
    monkeypatch.delenv("UAT_BOT_GID", raising=False)
    monkeypatch.delenv("RESEARCHER_BOT_GID", raising=False)
    monkeypatch.delenv("DOCWRITER_BOT_GID", raising=False)
    sys.modules.pop("asana_webhook", None)
    import asana_webhook as aw
    assert aw.BOT_USERS == {"1201234567890": "workitems"}

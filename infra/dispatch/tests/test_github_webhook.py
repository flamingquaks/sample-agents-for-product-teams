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
os.environ.setdefault("REGISTRY_PARAM", "/sdlc-agents/test/registry")
os.environ.setdefault("DISPATCH_FUNCTION", "dispatch-router-test")

SECRET = "s3cr3t-webhook-key"

# The receiver now resolves @mentions against the live registry (rendered from
# the capability rows), NOT a hardcoded roster — so an onboarded agent resolves
# with no code change. Mirror the router registry shape (agents + aliases).
REGISTRY = {
    "agents": {
        "workitems": {"aliases": ["pm", "status", "plan"]},
        "uat": {"aliases": ["qa", "test"]},
        "researcher": {"aliases": ["ba", "research", "analyze"]},
        "docwriter": {"aliases": ["docs", "doc", "writer"]},
        "adr": {"aliases": ["decisions", "architecture"]},
    }
}


def _fresh(monkeypatch, secret=SECRET, registry=REGISTRY):
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
            if Name == gw.REGISTRY_PARAM:
                return {"Parameter": {"Value": json.dumps(registry)}}
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


def test_ui_onboarded_agent_resolves_without_code_change(monkeypatch):
    # An agent id that is NOT in any hardcoded roster, present only in the
    # registry, must still resolve — the whole point of UI-driven onboarding.
    reg = {"agents": {"securityscan": {"aliases": ["sec"]}}}
    gw, dispatched = _fresh(monkeypatch, registry=reg)
    gw.handler(_event(_comment_body("@securityscan please review")))
    assert dispatched and dispatched[0]["agent_id"] == "securityscan"
    # And its registry-declared alias resolves to the canonical id too.
    gw2, dispatched2 = _fresh(monkeypatch, registry=reg)
    gw2.handler(_event(_comment_body("@sec please review")))
    assert dispatched2 and dispatched2[0]["agent_id"] == "securityscan"


def test_mention_not_in_registry_is_noop(monkeypatch):
    # A retired/unknown agent name (absent from the registry) does not dispatch.
    gw, dispatched = _fresh(monkeypatch, registry={"agents": {"workitems": {}}})
    resp = gw.handler(_event(_comment_body("@ghostagent do something")))
    assert resp["statusCode"] == 200
    assert not dispatched


def test_base64_encoded_body_verifies_and_dispatches(monkeypatch):
    import base64

    gw, dispatched = _fresh(monkeypatch)
    body = _comment_body("@workitems break this down", repo="acme/web", number=7)
    encoded = base64.b64encode(body.encode()).decode()
    # GitHub signs the DECODED bytes; the event carries the base64 form + flag.
    event = _event(body)  # signature computed over the raw (decoded) body
    event["body"] = encoded
    event["isBase64Encoded"] = True
    resp = gw.handler(event)
    assert resp["statusCode"] == 200
    assert dispatched and dispatched[0]["agent_id"] == "workitems"


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


# --- SCM notifications (spec §18.3) — pull_request / issues fan out, no dispatch


def _pr_body(action, *, repo="acme/web", number=5, merged=False, author="alice",
             draft=False, sender=None):
    return json.dumps(
        {
            "action": action,
            "pull_request": {"number": number, "title": "Add feature", "merged": merged,
                             "draft": draft, "user": {"login": author}},
            "repository": {"full_name": repo},
            # The delivery actor — the pusher on a synchronize. Defaults to the
            # PR author when unspecified (GitHub's usual shape for `opened`).
            "sender": {"login": sender if sender is not None else author},
        }
    )


def test_pr_opened_notifies_not_dispatches(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    calls = []
    monkeypatch.setattr(gw.notify, "notify", lambda **kw: calls.append(kw) or 1)
    resp = gw.handler(_event(_pr_body("opened"), event="pull_request"))
    assert resp["statusCode"] == 200
    assert not dispatched  # SCM events never dispatch an agent
    assert len(calls) == 1
    assert calls[0]["event"] == "pr_opened" and calls[0]["tier"] == "informative"
    assert calls[0]["repo"] == "acme/web" and calls[0]["unit"] == "pr:acme/web:5"


def test_pr_merged_notifies(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    calls = []
    monkeypatch.setattr(gw.notify, "notify", lambda **kw: calls.append(kw) or 1)
    gw.handler(_event(_pr_body("closed", merged=True), event="pull_request"))
    assert calls and calls[0]["event"] == "pr_merged"


def test_pr_closed_unmerged_does_not_notify(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    calls = []
    monkeypatch.setattr(gw.notify, "notify", lambda **kw: calls.append(kw) or 1)
    gw.handler(_event(_pr_body("closed", merged=False), event="pull_request"))
    assert not calls  # a closed-without-merge PR isn't a notify event


def test_review_requested_is_actionable(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    calls = []
    monkeypatch.setattr(gw.notify, "notify", lambda **kw: calls.append(kw) or 1)
    body = json.dumps(
        {
            "action": "review_requested",
            "pull_request": {"number": 3, "title": "Fix", "user": {"login": "alice"}},
            "requested_reviewer": {"login": "bob"},
            "repository": {"full_name": "acme/web"},
        }
    )
    gw.handler(_event(body, event="pull_request"))
    assert calls and calls[0]["tier"] == "actionable"
    assert calls[0]["actor"] == {"source": "github", "handle": "bob", "workspace": ""}


def test_issue_opened_notifies(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    calls = []
    monkeypatch.setattr(gw.notify, "notify", lambda **kw: calls.append(kw) or 1)
    body = json.dumps(
        {
            "action": "opened",
            "issue": {"number": 11, "title": "Bug", "user": {"login": "carol"}},
            "repository": {"full_name": "acme/web"},
        }
    )
    gw.handler(_event(body, event="issues"))
    assert calls and calls[0]["event"] == "issue_opened"


def test_scm_notify_failure_does_not_fail_webhook(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("slack down")

    monkeypatch.setattr(gw.notify, "notify", _boom)
    resp = gw.handler(_event(_pr_body("opened"), event="pull_request"))
    assert resp["statusCode"] == 200  # best-effort — never fails the delivery


# --- Auto-review (reviewer-agent spec §auto-trigger) — automation dispatch ----


def _quiet_notify(monkeypatch, gw):
    monkeypatch.setattr(gw.notify, "notify", lambda **kw: 1)


def test_pr_opened_runs_automation_after_notify(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    resp = gw.handler(_event(_pr_body("opened", author="alice"), event="pull_request"))
    assert resp["statusCode"] == 200
    assert len(calls) == 1
    assert calls[0]["connector"] == "github"
    assert calls[0]["event"] == "pull_request.opened"
    assert calls[0]["facts"]["repo"] == "acme/web"


def test_pr_synchronize_runs_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    gw.handler(_event(_pr_body("synchronize"), event="pull_request"))
    assert calls and calls[0]["event"] == "pull_request.synchronize"


def test_bot_authored_pr_skips_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    # A *[bot] author is skipped even without a configured slug (coarse backstop).
    gw.handler(_event(_pr_body("opened", author="sdlc-agents[bot]"), event="pull_request"))
    assert calls == []


def test_bot_guard_matches_configured_slug(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    # Configure the slug so its bot login is matched exactly.
    monkeypatch.setattr(gw, "GITHUB_APP_SLUG_PARAM", "/sdlc-agents/dev/github-app-slug")
    monkeypatch.setattr(gw, "_app_bot_login", lambda: "myapp[bot]")
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    gw.handler(_event(_pr_body("opened", author="myapp[bot]"), event="pull_request"))
    assert calls == []


def test_bot_pusher_on_human_pr_skips_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    # A fleet-bot PUSH to a human-opened PR: author stays the human, but the
    # delivery sender is the bot. The guard must skip on the sender too, or the
    # fleet's own push re-triggers a review (loop).
    body = _pr_body("synchronize", author="alice", sender="sdlc-agents[bot]")
    gw.handler(_event(body, event="pull_request"))
    assert calls == []


def test_draft_pr_skips_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    # Draft PRs are not auto-reviewed by default (spec §Non-goals). Stub the
    # repo-config read to the default (no opt-in) so the test stays offline.
    monkeypatch.setattr(gw, "_repo_reviews_drafts", lambda repo: False)
    gw.handler(_event(_pr_body("opened", draft=True), event="pull_request"))
    assert calls == []


def test_draft_pr_with_review_drafts_optin_runs_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    # The repo opted in via .pdlc-agents/review.yaml (spec §Repo config) — the
    # trigger layer honors it, so the draft PR IS auto-reviewed.
    monkeypatch.setattr(gw, "_repo_reviews_drafts", lambda repo: True)
    gw.handler(_event(_pr_body("opened", draft=True), event="pull_request"))
    assert len(calls) == 1
    assert calls[0]["event"] == "pull_request.opened"


def test_non_draft_pr_never_consults_repo_config(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)

    def _never(repo):
        raise AssertionError("_repo_reviews_drafts must not run for non-draft PRs")

    monkeypatch.setattr(gw, "_repo_reviews_drafts", _never)
    gw.handler(_event(_pr_body("opened", draft=False), event="pull_request"))
    assert len(calls) == 1  # automation ran without spending a config fetch


def test_pr_closed_does_not_run_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    # closed isn't an auto-review action → github_facts returns None → no call.
    gw.handler(_event(_pr_body("closed", merged=True), event="pull_request"))
    assert calls == []


def test_automation_failure_does_not_fail_webhook(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)

    def _boom(**kw):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(gw.automation, "match_and_dispatch", _boom)
    resp = gw.handler(_event(_pr_body("opened"), event="pull_request"))
    assert resp["statusCode"] == 200  # best-effort — never fails the delivery


def test_issues_event_does_not_run_pr_automation(monkeypatch):
    gw, dispatched = _fresh(monkeypatch)
    _quiet_notify(monkeypatch, gw)
    calls = []
    monkeypatch.setattr(gw.automation, "match_and_dispatch",
                        lambda **kw: calls.append(kw) or 1)
    body = json.dumps({
        "action": "opened",
        "issue": {"number": 11, "title": "Bug", "user": {"login": "carol"}},
        "repository": {"full_name": "acme/web"},
    })
    gw.handler(_event(body, event="issues"))
    assert calls == []  # automation only rides pull_request events


# --- _app_bot_login sentinel handling (template seeds "unset") --------------


def test_app_bot_login_unset_sentinel_yields_empty(monkeypatch):
    # infra/foundation/template.yaml seeds the slug param with Value: "unset";
    # before FIX the code only knew the "placeholder" sentinel and built a
    # bogus "unset[bot]" login that matches nothing.
    gw, _ = _fresh(monkeypatch)
    monkeypatch.setattr(gw, "GITHUB_APP_SLUG_PARAM", "/sdlc-agents/test/github-app-slug")

    class _SSM:
        def get_parameter(self, Name, WithDecryption=False):
            return {"Parameter": {"Value": "unset"}}

    monkeypatch.setattr(gw, "_ssm", _SSM())
    gw._slug_cache.update({"value": None, "expires_at": 0.0})
    assert gw._app_bot_login() == ""


# --- _repo_reviews_drafts (trigger-layer .pdlc-agents/review.yaml read) ------


def _stub_contents_api(monkeypatch, gw, *, status=200, body_text=None,
                       get_raises=None, token_raises=None):
    """Stub the modules _repo_reviews_drafts imports inside the function
    (github_app + requests are import-inside-function, so sys.modules injection
    is what the helper actually sees). Clears the per-repo TTL cache."""
    import types

    def _token(repo):
        if token_raises:
            raise token_raises
        return "test-installation-token"

    fake_gh = types.SimpleNamespace(
        GITHUB_API_BASE="https://api.github.com",
        installation_token_for_repo=_token,
    )

    class _Resp:
        status_code = status

        def json(self):
            import base64
            return {"content": base64.b64encode((body_text or "").encode()).decode()}

    def _get(url, headers=None, timeout=None):
        if get_raises:
            raise get_raises
        return _Resp()

    monkeypatch.setitem(sys.modules, "github_app", fake_gh)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_get))
    gw._drafts_cfg_cache.clear()


def test_repo_reviews_drafts_true_from_config(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw,
                       body_text="review_drafts: true\nmin_severity: low\n")
    assert gw._repo_reviews_drafts("acme/web") is True


def test_repo_reviews_drafts_absent_key_defaults_false(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw, body_text="min_severity: low\n")
    assert gw._repo_reviews_drafts("acme/web") is False


def test_repo_reviews_drafts_404_returns_false(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw, status=404)
    assert gw._repo_reviews_drafts("acme/web") is False


def test_repo_reviews_drafts_network_error_returns_false(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw, get_raises=RuntimeError("network down"))
    assert gw._repo_reviews_drafts("acme/web") is False


def test_repo_reviews_drafts_token_error_returns_false(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw, token_raises=RuntimeError("no installation"))
    assert gw._repo_reviews_drafts("acme/web") is False


def test_repo_reviews_drafts_malformed_yaml_returns_false(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw, body_text="review_drafts: [true\n  broken:")
    assert gw._repo_reviews_drafts("acme/web") is False


def test_repo_reviews_drafts_caches_per_repo(monkeypatch):
    gw, _ = _fresh(monkeypatch)
    _stub_contents_api(monkeypatch, gw, body_text="review_drafts: true\n")
    assert gw._repo_reviews_drafts("acme/web") is True
    # Second call within the TTL must serve from cache — swap the requests stub
    # (WITHOUT clearing the cache) for one that explodes if consulted.
    import types

    def _boom(url, headers=None, timeout=None):
        raise AssertionError("must be served from cache")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_boom))
    assert gw._repo_reviews_drafts("acme/web") is True

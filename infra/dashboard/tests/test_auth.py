"""Tests for the dashboard operator-authorization guard."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402


def _event(claims):
    return {"requestContext": {"authorizer": {"claims": claims}}}


def test_parse_groups_bracketed():
    assert auth.parse_groups({"cognito:groups": "[operators admins]"}) == {
        "operators",
        "admins",
    }


def test_parse_groups_comma():
    assert auth.parse_groups({"cognito:groups": "operators,viewers"}) == {
        "operators",
        "viewers",
    }


def test_parse_groups_single_and_empty():
    assert auth.parse_groups({"cognito:groups": "operators"}) == {"operators"}
    assert auth.parse_groups({"cognito:groups": ""}) == set()
    assert auth.parse_groups({}) == set()


def test_is_operator_true():
    assert (
        auth.is_operator(_event({"sub": "u1", "cognito:groups": "[operators]"})) is True
    )


def test_is_operator_false_without_group():
    assert auth.is_operator(_event({"sub": "u1", "cognito:groups": "viewers"})) is False


def test_is_operator_false_without_sub():
    assert auth.is_operator(_event({"cognito:groups": "operators"})) is False


def test_is_operator_false_no_authorizer():
    assert auth.is_operator({"requestContext": {}}) is False


def test_substring_group_does_not_grant():
    assert (
        auth.is_operator(_event({"sub": "u1", "cognito:groups": "operators-ro"}))
        is False
    )


def test_caller_sub():
    assert auth.caller_sub(_event({"sub": "abc"})) == "abc"
    assert auth.caller_sub({"requestContext": {}}) == ""


# --- admin group + operator/admin relationship ------------------------------


def test_admin_can_also_read():
    # admins are a superset of operators for API purposes.
    ev = _event({"sub": "u1", "cognito:groups": "[admins]"})
    assert auth.is_admin(ev) is True
    assert auth.is_operator(ev) is True


def test_operator_is_not_admin():
    ev = _event({"sub": "u1", "cognito:groups": "[operators]"})
    assert auth.is_operator(ev) is True
    assert auth.is_admin(ev) is False


def test_is_admin_false_without_group_or_sub():
    assert auth.is_admin(_event({"sub": "u1", "cognito:groups": "operators"})) is False
    assert auth.is_admin(_event({"cognito:groups": "admins"})) is False
    assert auth.is_admin({"requestContext": {}}) is False


def test_admin_substring_does_not_grant():
    assert auth.is_admin(_event({"sub": "u1", "cognito:groups": "admins-ro"})) is False


# --- AVP-backed authorization path ------------------------------------------
# When AVP_POLICY_STORE_ID is set, decisions come from AVP.is_authorized. These
# stub the client and assert the request shape + fail-closed behavior. (Above
# tests exercise the local-fallback path, with the env var unset.)


class _FakeAVP:
    def __init__(self, decision="ALLOW", raise_exc=False):
        self.decision = decision
        self.raise_exc = raise_exc
        self.last = None

    def is_authorized(self, **kw):
        self.last = kw
        if self.raise_exc:
            raise RuntimeError("AVP unavailable")
        return {"decision": self.decision}


def _use_avp(monkeypatch, fake):
    monkeypatch.setenv("AVP_POLICY_STORE_ID", "ps-123")
    monkeypatch.setattr(auth, "_avp_client", lambda: fake)


def test_avp_allow_grants(monkeypatch):
    fake = _FakeAVP(decision="ALLOW")
    _use_avp(monkeypatch, fake)
    ev = _event({"sub": "u1", "cognito:groups": "[operators]"})
    assert auth.is_operator(ev) is True
    # Request shape: principal is the User(sub); groups passed as parent entities.
    assert fake.last["policyStoreId"] == "ps-123"
    assert fake.last["principal"] == {"entityType": "SdlcDashboard::User", "entityId": "u1"}
    assert fake.last["action"] == {"actionType": "SdlcDashboard::Action", "actionId": "Read"}
    parents = fake.last["entities"]["entityList"][0]["parents"]
    assert {"entityType": "SdlcDashboard::Group", "entityId": "operators"} in parents


def test_avp_deny_denies(monkeypatch):
    _use_avp(monkeypatch, _FakeAVP(decision="DENY"))
    ev = _event({"sub": "u1", "cognito:groups": "[operators]"})
    assert auth.is_admin(ev) is False


def test_avp_error_fails_closed(monkeypatch):
    _use_avp(monkeypatch, _FakeAVP(raise_exc=True))
    ev = _event({"sub": "u1", "cognito:groups": "[admins]"})
    assert auth.is_operator(ev) is False
    assert auth.is_admin(ev) is False


def test_avp_no_subject_denies_without_calling(monkeypatch):
    fake = _FakeAVP(decision="ALLOW")
    _use_avp(monkeypatch, fake)
    assert auth.is_operator({"requestContext": {}}) is False
    assert fake.last is None  # denied before any AVP call


def test_avp_write_action_id(monkeypatch):
    fake = _FakeAVP(decision="ALLOW")
    _use_avp(monkeypatch, fake)
    auth.is_admin(_event({"sub": "u1", "cognito:groups": "[admins]"}))
    assert fake.last["action"]["actionId"] == "Write"

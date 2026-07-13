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

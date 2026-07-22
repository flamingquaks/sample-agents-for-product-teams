"""Tests for the build-time requirements generator (agents/_base/gen_requirements.py).

Runs inside CodeBuild for a CUSTOM agent: reads a DynamoDB get-item document on
stdin and prints one validated pip specifier per line. These pin (a) plain
specifiers pass through, (b) the §7.1 forbidden shapes are rejected with a
non-zero exit even though they were persisted (defense in depth against a row
written outside the admin API), and (c) an empty/absent row yields nothing.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(stdin_text, monkeypatch, capsys):
    sys.modules.pop("gen_requirements", None)
    import gen_requirements as gr

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    gr.main()
    return capsys.readouterr().out


def _item(reqs):
    return json.dumps({"Item": {"requirements": {"L": [{"S": r} for r in reqs]}}})


def test_plain_specifiers_pass_through(monkeypatch, capsys):
    out = _run(_item(["tavily-python", "requests>=2.31.0", "pkg[extra]<1.0; python_version>='3.10'"]),
               monkeypatch, capsys)
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines == ["tavily-python", "requests>=2.31.0", "pkg[extra]<1.0; python_version>='3.10'"]


def test_empty_stdin_emits_nothing(monkeypatch, capsys):
    assert _run("", monkeypatch, capsys).strip() == ""


def test_absent_requirements_emits_nothing(monkeypatch, capsys):
    assert _run(json.dumps({"Item": {}}), monkeypatch, capsys).strip() == ""


@pytest.mark.parametrize("bad", [
    "--index-url http://evil",
    "-e .",
    "https://evil.com/pkg.whl",
    "git+https://evil/pkg.git",
    "pkg @ git+https://evil/pkg.git",
    "file:///etc/passwd",
    "./local",
    "../local",
    "/abs/local",
])
def test_forbidden_specifiers_exit_nonzero(bad, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(_item([bad]), monkeypatch, capsys)
    assert exc.value.code == 1


@pytest.mark.parametrize("bad", [
    # Control characters (finding 0): a newline inside one "requirement" would
    # emit a second physical line into requirements-extra.txt that pip parses as
    # a standalone global option, defeating the §7.1 allowlist.
    "requests\n--index-url http://evil",
    "requests\r\n-e .",
    "requests\t>=2.31",
    "req\x00uests",
])
def test_control_characters_exit_nonzero(bad, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(_item([bad]), monkeypatch, capsys)
    assert exc.value.code == 1


def test_pep508_internal_spaces_accepted(monkeypatch, capsys):
    """PEP 508 allows spaces around the version constraint — the admin boundary
    accepts these, so the build-side re-validation must too (finding 5)."""
    out = _run(_item(["requests >= 2.31", "pkg[all] >= 0.5, < 1.0"]), monkeypatch, capsys)
    assert out.splitlines() == ["requests >= 2.31", "pkg[all] >= 0.5, < 1.0"]


def _item_with_review(reqs, review_status):
    return json.dumps({"Item": {
        "requirements": {"L": [{"S": r} for r in reqs]},
        "review_status": {"S": review_status},
    }})


def test_pending_review_row_refused_when_gate_on(monkeypatch, capsys):
    """Build-side backstop (§7.5): with the gate on, a pending_review row's
    unapproved requirements must not be materialized — refuse (exit 1)."""
    monkeypatch.setenv("REQUIRE_AGENT_APPROVAL", "true")
    with pytest.raises(SystemExit) as exc:
        _run(_item_with_review(["requests>=2.31"], "pending_review"), monkeypatch, capsys)
    assert exc.value.code == 1


def test_pending_review_row_allowed_when_gate_off(monkeypatch, capsys):
    monkeypatch.setenv("REQUIRE_AGENT_APPROVAL", "false")
    out = _run(_item_with_review(["requests>=2.31"], "pending_review"), monkeypatch, capsys)
    assert out.splitlines() == ["requests>=2.31"]


def test_approved_row_materializes_with_gate_on(monkeypatch, capsys):
    monkeypatch.setenv("REQUIRE_AGENT_APPROVAL", "true")
    out = _run(_item_with_review(["requests>=2.31"], "approved"), monkeypatch, capsys)
    assert out.splitlines() == ["requests>=2.31"]

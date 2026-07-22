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

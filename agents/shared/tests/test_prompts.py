"""Unit tests for shared.prompts.compose_system_prompt.

The key guarantee is that assembly is pure string substitution: a literal brace
in any rule/template text must NOT crash (the old plumbing did .replace then a
downstream .format, which raised on a stray '{' / '}').
"""

import sys
from pathlib import Path

# agents/ on sys.path so `import shared.prompts` resolves the shared package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.prompts import compose_system_prompt  # noqa: E402

ASANA = "ASANA-MODE {shared_rules} ctx={project_context}"
GITHUB = "GITHUB-MODE {shared_rules} ctx={project_context}"
RULES = "RULE-BLOCK"
CTX = "CTX-BLOCK"


def test_selects_github_variant():
    out = compose_system_prompt("github", ASANA, GITHUB, RULES, CTX)
    assert out.startswith("GITHUB-MODE")
    assert "RULE-BLOCK" in out and "ctx=CTX-BLOCK" in out


def test_selects_asana_variant_for_asana_and_default():
    for backend in ("asana", "", None, "trello"):
        out = compose_system_prompt(backend, ASANA, GITHUB, RULES, CTX)
        assert out.startswith("ASANA-MODE")


def test_no_placeholders_remain():
    out = compose_system_prompt("asana", ASANA, GITHUB, RULES, CTX)
    assert "{shared_rules}" not in out
    assert "{project_context}" not in out


def test_braces_in_content_do_not_crash():
    # The whole point of the refactor: a JSON-ish example with literal braces in
    # the rules or context must be substituted literally, not interpreted.
    rules_with_braces = 'Return JSON like {"key": "value"} exactly.'
    ctx_with_braces = 'Board fields: {status} {priority}'
    out = compose_system_prompt("github", ASANA, GITHUB, rules_with_braces, ctx_with_braces)
    assert '{"key": "value"}' in out
    assert "{status} {priority}" in out

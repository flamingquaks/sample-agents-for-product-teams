"""Parity contract between the two §7.1 requirements validators.

The admin API validates a capability's ``requirements`` at the boundary
(admin._valid_requirement_spec); the build re-validates each specifier as the
last step before pip runs (agents/_base/gen_requirements.py). The two MUST make
identical accept/reject decisions — a divergence means a spec passes onboarding
then fails the build (review finding: whitespace mismatch), or worse, a spec the
API rejects is accepted by a non-API path. This suite pins the shared decision
procedure over a corpus of good and bad specifiers, and asserts the regex /
substring lists themselves are byte-identical.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DASH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_DASH))

_GEN_PATH = _DASH.parents[1] / "agents" / "_base" / "gen_requirements.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_requirements", _GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gen_accepts(gen, spec_str: str) -> bool:
    """Whether gen_requirements._validate accepts (it sys.exit(1)s on reject)."""
    try:
        gen._validate(spec_str)
        return True
    except SystemExit:
        return False


ACCEPT = [
    "requests",
    "requests>=2.31",
    "requests >= 2.31",  # PEP 508 allows spaces around the constraint
    "tavily-python[all]>=0.5,<1.0",
    "tavily-python[all] >= 0.5, < 1.0",
    "pkg[extra1,extra2]==1.0",
    "pkg; python_version >= '3.10'",
    "Pkg_Name.mixed-Case~=2.0",
]

REJECT = [
    "",
    "--index-url http://evil.com",
    "-e git+https://foo",
    "-r other.txt",
    "https://evil.com/pkg.whl",
    "git+https://evil.com/pkg.git",
    "GIT+HTTPS://evil.com/pkg.git",
    "mypkg @ git+https://evil.com/pkg.git",
    "svn+https://evil.com/pkg",
    "file:///etc/passwd",
    "pkg @ file:///etc/passwd",
    "./local-evil",
    "../local-evil",
    "/abs/local-evil",
    # Control characters: a newline would emit a second physical line into
    # requirements-extra.txt that pip parses as a global option (finding 0).
    "requests\n--index-url http://evil.com",
    "requests\r\n-e .",
    "requests\t>=2.31",
    "req\x00uests",
    # Per-requirement pip options attach after whitespace — the version-tail
    # regex would swallow them, so the " -" check must reject.
    "somepkg>=0 --hash=sha256:aaaa",
    "somepkg>=0 --config-settings=--build-option=x",
    "somepkg --index-url http://evil",
]


@pytest.fixture(scope="module")
def validators():
    import admin

    return admin._valid_requirement_spec, _load_gen()


@pytest.mark.parametrize("spec_str", ACCEPT)
def test_both_accept(validators, spec_str):
    admin_valid, gen = validators
    assert admin_valid(spec_str), f"admin should accept {spec_str!r}"
    assert _gen_accepts(gen, spec_str), f"gen_requirements should accept {spec_str!r}"


@pytest.mark.parametrize("spec_str", REJECT)
def test_both_reject(validators, spec_str):
    admin_valid, gen = validators
    assert not admin_valid(spec_str), f"admin should reject {spec_str!r}"
    assert not _gen_accepts(gen, spec_str), f"gen_requirements should reject {spec_str!r}"


def test_regex_and_substring_lists_identical(validators):
    """The CONTRACT note on both validators: the decision artifacts themselves
    must be byte-identical so the corpus above can never drift apart silently."""
    import admin

    gen = validators[1]
    assert admin._REQ_SPECIFIER_RE.pattern == gen._SPECIFIER_RE.pattern
    assert tuple(admin._REQ_FORBIDDEN_SUBSTRINGS) == tuple(gen._FORBIDDEN_SUBSTRINGS)

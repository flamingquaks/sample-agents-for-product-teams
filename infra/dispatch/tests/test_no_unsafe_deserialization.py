"""Repo-wide guard: no unsafe YAML/pickle deserialization anywhere (ACAT
UnsafeYAMLLoad, CWE-20/74/94).

Scans every Python source in the repo for the call patterns that deserialize
attacker-controllable input into arbitrary objects:

  - yaml.load / yaml.full_load / yaml.unsafe_load, and the unsafe Loader=
    classes (FullLoader constructs arbitrary Python objects via tags;
    UnsafeLoader is yaml.load's pre-5.1 behavior). Everything must go through
    yaml.safe_load or a SafeLoader subclass.
  - pickle/marshal/shelve loads and jsonpickle — never acceptable on data that
    crosses a trust boundary, and this codebase has no legitimate use at all.

A comment mentioning a pattern doesn't trip the scan (only code lines count),
so the SafeLoader-subclass sites that explain WHY they avoid yaml.load stay
green.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# Pattern → why it's forbidden. Matched against code with comments stripped.
FORBIDDEN = {
    r"\byaml\.load\(": "yaml.load — use yaml.safe_load or drive a SafeLoader subclass",
    r"\byaml\.full_load\(": "yaml.full_load constructs arbitrary objects",
    r"\byaml\.unsafe_load\(": "yaml.unsafe_load is RCE on untrusted input",
    r"\bFullLoader\b": "FullLoader constructs arbitrary objects via tags",
    r"\bUnsafeLoader\b": "UnsafeLoader is RCE on untrusted input",
    r"\bpickle\.loads?\(": "pickle deserialization executes arbitrary code",
    r"\bmarshal\.loads?\(": "marshal deserialization is unsafe",
    r"\bshelve\.open\(": "shelve is pickle-backed",
    r"\bjsonpickle\b": "jsonpickle reconstructs arbitrary objects",
}

SKIP_DIRS = {".aws-sam", "node_modules", ".git", "__pycache__", ".pytest_cache", "build", "dist", "site-packages"}


def _skip(path: Path) -> bool:
    # Skip vendored/venv trees (any dir named like a venv or containing
    # site-packages) — third-party code is the dependency scanner's job; this
    # guard is for OUR sources.
    return any(
        part in SKIP_DIRS or part.startswith(".venv") or part == "venv"
        for part in path.parts
    )


def _code_lines(path: Path):
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        code = line.split("#", 1)[0]
        if code.strip():
            yield lineno, code


def test_no_unsafe_deserialization_repo_wide():
    offenders = []
    this_file = Path(__file__).resolve()
    for py in REPO.rglob("*.py"):
        # Skip this guard itself — its docstring + pattern table necessarily
        # NAME the forbidden calls (strings/docstrings aren't '#' comments, so
        # the comment-stripper doesn't clear them).
        if _skip(py) or py.resolve() == this_file:
            continue
        for lineno, code in _code_lines(py):
            for pattern, why in FORBIDDEN.items():
                if re.search(pattern, code):
                    offenders.append(f"{py.relative_to(REPO)}:{lineno} — {why}\n    {code.strip()}")
    assert not offenders, "unsafe deserialization found:\n" + "\n".join(offenders)

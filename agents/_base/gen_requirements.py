"""Build-time helper: emit a capability's per-agent pip requirements (spec §5).

Runs inside the shared CodeBuild project (buildspec in the foundation template),
NOT in the agent runtime. Reads a DynamoDB ``get-item`` response for the
capability row on stdin and prints one validated pip specifier per line, which the
buildspec redirects into ``agents/_base/requirements-extra.txt`` for the generic
base image to ``pip install -r``.

Why read the row (not a build env override): the weekly security rebuild restarts
a build with only ``AGENT_NAME`` set, so the requirements must come from the
authoritative capability row, not a per-build parameter.

Security (§7.1, defense in depth): the admin API already rejects anything that
isn't a plain PyPI specifier before the row is written, but this is the last step
before ``pip install`` actually runs in the build container, so it RE-VALIDATES
every specifier and exits non-zero on anything suspicious. A row written outside
the API (a future non-API path, a manual DynamoDB edit) therefore still can't slip
a ``--index-url``/VCS/URL/local-path spec into the build. stdin is a DynamoDB
get-item JSON document, so nothing here is shell-interpolated.
"""

import json
import os
import re
import sys

# A plain PyPI specifier: a package name, optional extras, optional version
# constraints and environment markers (PEP 508 allows spaces around each part,
# e.g. ``requests >= 2.31``). Deliberately conservative — anything with a flag,
# URL, VCS scheme, or path is rejected.
#
# CONTRACT: this regex + the forbidden-substring / control-character checks MUST
# stay byte-identical with admin._valid_requirement_spec
# (infra/dashboard/admin.py) — the API-boundary validator. If they diverge, a
# spec can pass onboarding then fail the build (or vice versa). The parity test
# (infra/dashboard/tests/test_requirements_parity.py) enforces this.
_SPECIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"           # package name
    r" *(\[[A-Za-z0-9,. _-]+\])?"             # optional extras
    r" *([<>=!~][=]?[^;]*)?"                  # optional version constraint(s)
    r"(;.*)?$"                                # optional environment marker
)
_FORBIDDEN_SUBSTRINGS = ("://", "@", "git+", "svn+", "hg+", "bzr+")


def _unwrap(attr: dict):
    """Unwrap one DynamoDB AttributeValue into a Python value (only the shapes a
    capability row uses: strings and lists-of-strings)."""
    if "S" in attr:
        return attr["S"]
    if "L" in attr:
        return [_unwrap(x) for x in attr["L"]]
    return None


def _validate(spec: str) -> str:
    stripped = spec.strip()
    lowered = stripped.lower()
    if (
        not stripped
        # A control character (\n, \r, \t, …) inside the spec would let one
        # "requirement" emit a second physical line into requirements-extra.txt
        # that pip parses as a standalone global option (e.g. --index-url),
        # defeating the §7.1 allowlist. Rejected before anything else.
        or any(ord(c) < 32 or ord(c) == 127 for c in stripped)
        or stripped.startswith(("-", "/", "./", "../"))
        # Whitespace-then-dash is how pip's PER-REQUIREMENT options attach
        # ("pkg>=1 --hash=…", "--config-settings=…"): the leading-dash check
        # above misses them and the version-tail regex would swallow them. No
        # legitimate specifier/marker contains " -".
        or re.search(r"\s-", stripped)
        or any(bad in lowered for bad in _FORBIDDEN_SUBSTRINGS)
        or not _SPECIFIER_RE.match(stripped)
    ):
        sys.stderr.write(
            f"gen_requirements: refusing non-plain pip specifier {spec!r} — "
            "only name[extras]<op>version[; marker] is allowed (§7.1)\n"
        )
        sys.exit(1)
    return stripped


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        return  # no row on stdin → empty extras file
    item = (json.loads(raw) or {}).get("Item") or {}
    # Approval-gate backstop (§7.5, defense in depth): if this row is parked
    # pending_review while the gate is on, its requirements are UNAPPROVED. The
    # admin/rebuilder paths already avoid starting a build for such a row, but
    # this is the last step before pip runs, so refuse here too — a build
    # triggered by any other path (a future non-API trigger, a manual
    # StartBuild) still can't pip-install deps that no second admin approved.
    gate_on = os.environ.get("REQUIRE_AGENT_APPROVAL", "true").lower() == "true"
    review_status = _unwrap(item.get("review_status", {}))
    if gate_on and review_status == "pending_review":
        sys.stderr.write(
            "gen_requirements: capability is pending_review — refusing to "
            "materialize unapproved requirements (§7.5)\n"
        )
        sys.exit(1)
    reqs = _unwrap(item.get("requirements", {})) or []
    for spec in reqs:
        if isinstance(spec, str):
            print(_validate(spec))


if __name__ == "__main__":
    main()

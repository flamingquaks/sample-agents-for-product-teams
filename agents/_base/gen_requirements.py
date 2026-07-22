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
import re
import sys

# A plain PyPI specifier: a package name, optional extras, optional version
# constraints and environment markers. Deliberately conservative — anything with
# a flag, URL, VCS scheme, or path is rejected. Mirrors the intent of the admin
# API's _FORBIDDEN_REQ_PREFIXES check (admin._validate_capability_body).
_SPECIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"          # package name
    r"(\[[A-Za-z0-9,._-]+\])?"               # optional extras
    r"([<>=!~][=]?[^;]*)?"                    # optional version constraint(s)
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
        or stripped.startswith(("-", "/", "./", "../"))
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
    reqs = _unwrap(item.get("requirements", {})) or []
    for spec in reqs:
        if isinstance(spec, str):
            print(_validate(spec))


if __name__ == "__main__":
    main()

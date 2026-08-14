"""Finding fingerprints — stable across rebases so the don't-re-raise ledger
survives force-pushes.

A fingerprint anchors a finding to its file + the NORMALIZED content of the
hunk it sits on + a short rule slug, NOT to line numbers (which shift on every
rebase). Two runs that flag the same defect on the same code produce the same
fingerprint even if the surrounding diff moved.
"""

import hashlib
import re

_WS_RE = re.compile(r"\s+")


def normalize_anchor(hunk_text: str) -> str:
    """Collapse a hunk's added/context lines to whitespace-insensitive content,
    dropping diff markers and blank lines — so reindentation or a moved hunk
    doesn't change the fingerprint."""
    lines = []
    for raw in (hunk_text or "").splitlines():
        # Drop hunk headers and pure-removal lines; keep added/context content.
        if raw.startswith("@@") or raw.startswith("-"):
            continue
        body = raw[1:] if raw[:1] in "+ " else raw
        norm = _WS_RE.sub(" ", body).strip()
        if norm:
            lines.append(norm)
    return "\n".join(lines)


def finding_fingerprint(file_path: str, hunk_text: str, rule: str) -> str:
    """Stable id for a finding: sha256 over (file, normalized hunk, rule slug).
    Truncated to 16 hex chars — collision-safe at the per-PR scale, compact in
    the ledger."""
    rule_slug = _WS_RE.sub("-", (rule or "").strip().lower())[:48]
    basis = f"{file_path}\x00{normalize_anchor(hunk_text)}\x00{rule_slug}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

"""Format the Reviewer's inline findings and summary comment deterministically.

Keeping the wire format in a tool (not the prompt) makes the signature, severity
tags, and failure-scenario discipline reproducible and reviewable — the same
reason Adr formats its rationale in a tool. The min_severity floor and
max_findings cap are enforced HERE too (not just in the prompt), so a model
that ignores its instructions still can't post a wall of nits.
"""

from strands import tool

from project_config import DEFAULT_MAX_FINDINGS, DEFAULT_MIN_SEVERITY, SEVERITY_ORDER

SIGNATURE = "🔎 **[Reviewer]**"

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
_SEV_LABEL = {"high": "🔴 high", "medium": "🟠 medium", "low": "🟡 low"}


def _coerce_severity(value) -> str:
    """Normalize a severity value; unknown/missing coerces to "low"."""
    sev = str(value or "low").strip().lower()
    return sev if sev in _SEV_ORDER else "low"


@tool
def format_review(findings: list[dict], files_reviewed: int, dropped_overflow: int = 0,
                  ignored_files: list[str] = None,
                  min_severity: str = DEFAULT_MIN_SEVERITY,
                  max_findings: int = DEFAULT_MAX_FINDINGS) -> dict:
    """Build the PR review payload: inline comments + a summary body.

    Enforcement order (this is the contract):
      1. Drop findings without a `failure_scenario` (spec hard rule).
      2. Coerce unknown/missing severity to "low", THEN drop findings below
         `min_severity` (coercion first so an unknown severity can't dodge the
         floor). The dropped count is noted in the summary.
      3. Sort by severity, most severe first.
      4. Truncate to `max_findings`, keeping the most severe; the truncated
         count is added to `dropped_overflow` so the summary stays accurate.

    Args:
        findings: [{path, line, severity, title, failure_scenario, suggestion}].
            A finding without `failure_scenario` is DROPPED here (spec hard rule)
            — a defect you can't describe a failure for isn't a finding.
        files_reviewed: How many changed files you actually reviewed.
        dropped_overflow: Findings the caller already dropped for exceeding
            max_findings; summed with any truncated here (noted in the summary
            so the cap is never silent).
        ignored_files: Paths skipped by ignore_paths (noted in the summary).
        min_severity: Repo severity floor (low | medium | high); findings below
            it are dropped here, and the drop is noted in the summary.
        max_findings: Hard cap per review, enforced here after sorting so the
            most severe findings survive.

    Returns:
        {
          "comments": [{path, line, body}],   # inline review comments
          "summary": str,                      # review body / or lone comment
          "has_findings": bool,                # kept findings only
          "counts": {"high": n, "medium": n, "low": n},  # kept findings only
        }
    """
    # 1. No failure scenario, no finding.
    kept = [f for f in (findings or []) if (f.get("failure_scenario") or "").strip()]

    # 2. Coerce severity, then apply the min_severity floor.
    floor = str(min_severity or DEFAULT_MIN_SEVERITY).strip().lower()
    if floor not in SEVERITY_ORDER:
        floor = DEFAULT_MIN_SEVERITY
    floor_rank = SEVERITY_ORDER.index(floor)
    dropped_below_floor = 0
    surviving = []
    for f in kept:
        sev = _coerce_severity(f.get("severity"))
        if SEVERITY_ORDER.index(sev) < floor_rank:
            dropped_below_floor += 1
            continue
        surviving.append({**f, "severity": sev})
    kept = surviving

    # 3. Most severe first.
    kept.sort(key=lambda f: _SEV_ORDER[f["severity"]])

    # 4. Cap, keeping the most severe (sort guarantees that).
    if len(kept) > max_findings:
        dropped_overflow += len(kept) - max_findings
        kept = kept[:max_findings]

    counts = {"high": 0, "medium": 0, "low": 0}
    comments = []
    for f in kept:
        sev = f["severity"]
        counts[sev] += 1
        body = (
            f"{SIGNATURE} {_SEV_LABEL[sev]}: {f.get('title', '').strip()}\n\n"
            f"**Failure scenario:** {f['failure_scenario'].strip()}"
        )
        suggestion = (f.get("suggestion") or "").strip()
        if suggestion:
            body += f"\n\n{suggestion}"
        comments.append({"path": f.get("path", ""), "line": f.get("line"), "body": body})

    tail = ""
    if dropped_below_floor:
        tail += (f" {dropped_below_floor} finding(s) below the min_severity floor "
                 f"dropped.")
    if dropped_overflow:
        tail += f" {dropped_overflow} lower-severity finding(s) dropped for the per-review cap."
    if ignored_files:
        tail += f" Skipped {len(ignored_files)} ignored path(s)."

    if not comments:
        summary = (
            f"{SIGNATURE} Reviewed {files_reviewed} changed file(s). Nothing rose "
            f"above the severity threshold.{tail}"
        )
        return {"comments": [], "summary": summary, "has_findings": False, "counts": counts}

    summary = (
        f"{SIGNATURE} Reviewed {files_reviewed} changed file(s). "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low. "
        f"Inline comments are on the specific diff lines.{tail}"
    )
    return {"comments": comments, "summary": summary, "has_findings": True, "counts": counts}

"""Format the Reviewer's inline findings and summary comment deterministically.

Keeping the wire format in a tool (not the prompt) makes the signature, severity
tags, and failure-scenario discipline reproducible and reviewable — the same
reason Adr formats its rationale in a tool.
"""

from strands import tool

SIGNATURE = "🔎 **[Reviewer]**"

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
_SEV_LABEL = {"high": "🔴 high", "medium": "🟠 medium", "low": "🟡 low"}


@tool
def format_review(findings: list[dict], files_reviewed: int, dropped_overflow: int = 0,
                  ignored_files: list[str] = None) -> dict:
    """Build the PR review payload: inline comments + a summary body.

    Args:
        findings: [{path, line, severity, title, failure_scenario, suggestion}].
            A finding without `failure_scenario` is DROPPED here (spec hard rule)
            — a defect you can't describe a failure for isn't a finding.
        files_reviewed: How many changed files you actually reviewed.
        dropped_overflow: Findings dropped for exceeding max_findings (noted in
            the summary so the cap is never silent).
        ignored_files: Paths skipped by ignore_paths (noted in the summary).

    Returns:
        {
          "comments": [{path, line, body}],   # inline review comments
          "summary": str,                      # review body / or lone comment
          "has_findings": bool,
          "counts": {"high": n, "medium": n, "low": n},
        }
    """
    kept = [f for f in (findings or []) if (f.get("failure_scenario") or "").strip()]
    kept.sort(key=lambda f: _SEV_ORDER.get(str(f.get("severity", "low")).lower(), 3))

    counts = {"high": 0, "medium": 0, "low": 0}
    comments = []
    for f in kept:
        sev = str(f.get("severity", "low")).lower()
        if sev not in counts:
            sev = "low"
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

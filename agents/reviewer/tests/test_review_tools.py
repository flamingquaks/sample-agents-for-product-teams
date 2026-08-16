"""Unit tests for Reviewer's deterministic tools: diff chunking + ignore-glob
filtering (plan_review), rebase-stable fingerprints (fingerprint), and the
review wire format incl. the no-failure-scenario drop (format_review)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fingerprint import finding_fingerprint, normalize_anchor  # noqa: E402
from tools.format_review import format_review  # noqa: E402
from tools.plan_review import plan_review  # noqa: E402

# strands @tool wraps the function; call the underlying callable when present.
_plan = getattr(plan_review, "func", None) or getattr(plan_review, "_tool_func", None) or plan_review
_fmt = getattr(format_review, "func", None) or getattr(format_review, "_tool_func", None) or format_review


DIFF = """\
diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@ def handler(x):
     y = x + 1
+    z = y / 0
     return y
diff --git a/poetry.lock b/poetry.lock
index aaa..bbb 100644
--- a/poetry.lock
+++ b/poetry.lock
@@ -1,2 +1,3 @@
+churn
diff --git a/dist/bundle.js b/dist/bundle.js
index ccc..ddd 100644
--- a/dist/bundle.js
+++ b/dist/bundle.js
@@ -1 +1 @@
+minified
"""


def test_plan_review_splits_files_and_applies_ignore_globs():
    out = _plan(DIFF, ignore_paths=["*.lock", "dist/**"], max_findings=5)
    paths = [u["path"] for u in out["units"]]
    assert paths == ["src/app.py"]  # lockfile + dist bundle ignored
    assert set(out["ignored_files"]) == {"poetry.lock", "dist/bundle.js"}
    assert out["file_count"] == 1
    assert out["max_findings"] == 5
    # The reviewable file carries its hunk and the new-file start line.
    unit = out["units"][0]
    assert unit["hunks"] and "z = y / 0" in unit["hunks"][0]
    assert unit["hunk_start_lines"] == [10]


def test_plan_review_empty_diff():
    out = _plan("", ignore_paths=[], max_findings=10)
    assert out["units"] == [] and out["file_count"] == 0


def test_fingerprint_is_stable_across_line_shifts_and_reindent():
    # Same content, different leading line numbers + indentation ⇒ same id.
    a = "@@ -10,3 +10,4 @@\n+    z = compute(y)\n     return y"
    b = "@@ -80,3 +80,4 @@\n+        z = compute(y)\n     return y"
    assert finding_fingerprint("src/app.py", a, "div-by-zero") == \
        finding_fingerprint("src/app.py", b, "div-by-zero")


def test_fingerprint_differs_by_file_and_rule():
    hunk = "@@ -1 +1 @@\n+x = 1"
    assert finding_fingerprint("a.py", hunk, "r1") != finding_fingerprint("b.py", hunk, "r1")
    assert finding_fingerprint("a.py", hunk, "r1") != finding_fingerprint("a.py", hunk, "r2")


def test_normalize_anchor_drops_markers_and_removals():
    norm = normalize_anchor("@@ -1,2 +1,2 @@\n-old line\n+new  line\n unchanged")
    assert "old line" not in norm
    assert "new line" in norm and "unchanged" in norm


def test_format_review_drops_findings_without_failure_scenario():
    findings = [
        {"path": "a.py", "line": 5, "severity": "high", "title": "divide by zero",
         "failure_scenario": "x=-1 → ZeroDivisionError", "suggestion": "```suggestion\nz = y\n```"},
        {"path": "a.py", "line": 9, "severity": "low", "title": "vague nit",
         "failure_scenario": ""},  # no scenario → dropped
    ]
    out = _fmt(findings, files_reviewed=1)
    assert out["has_findings"] is True
    assert len(out["comments"]) == 1
    assert out["counts"] == {"high": 1, "medium": 0, "low": 0}
    assert "🔎 **[Reviewer]**" in out["comments"][0]["body"]
    assert "Failure scenario" in out["comments"][0]["body"]


def test_format_review_always_leaves_one_artifact_when_empty():
    out = _fmt([], files_reviewed=3, ignored_files=["a.lock"])
    assert out["has_findings"] is False
    assert out["comments"] == []
    assert "Nothing rose above" in out["summary"]
    assert "🔎 **[Reviewer]**" in out["summary"]


def test_format_review_sorts_by_severity_and_notes_overflow():
    findings = [
        {"path": "a.py", "line": 1, "severity": "low", "title": "l",
         "failure_scenario": "s"},
        {"path": "a.py", "line": 2, "severity": "high", "title": "h",
         "failure_scenario": "s"},
    ]
    out = _fmt(findings, files_reviewed=1, dropped_overflow=3)
    assert out["comments"][0]["body"].count("high") >= 1  # high sorted first
    assert "3 lower-severity finding(s) dropped" in out["summary"]


def _finding(sev, line=1, title="t"):
    return {"path": "a.py", "line": line, "severity": sev, "title": title,
            "failure_scenario": "bad input → wrong output"}


def test_format_review_min_severity_floor_drops_and_notes():
    findings = [_finding("high", 1), _finding("low", 2), _finding("medium", 3)]
    out = _fmt(findings, files_reviewed=1, min_severity="medium")
    assert len(out["comments"]) == 2
    assert out["counts"] == {"high": 1, "medium": 1, "low": 0}
    assert "1 finding(s) below the min_severity floor dropped." in out["summary"]


def test_format_review_unknown_severity_coerces_to_low_and_hits_floor():
    # Unknown severity coerces to low BEFORE the floor test, so it can't dodge it.
    out = _fmt([_finding("bogus")], files_reviewed=1, min_severity="medium")
    assert out["has_findings"] is False
    assert out["comments"] == []
    assert out["counts"] == {"high": 0, "medium": 0, "low": 0}
    assert "1 finding(s) below the min_severity floor dropped." in out["summary"]
    # With the default floor (low), the same finding survives as a low.
    out = _fmt([_finding("bogus")], files_reviewed=1)
    assert out["counts"] == {"high": 0, "medium": 0, "low": 1}
    assert "🟡 low" in out["comments"][0]["body"]


def test_format_review_cap_truncates_keeping_most_severe():
    findings = [_finding("low", 1), _finding("high", 2), _finding("medium", 3)]
    out = _fmt(findings, files_reviewed=1, dropped_overflow=2, max_findings=2)
    assert len(out["comments"]) == 2
    assert "🔴 high" in out["comments"][0]["body"]
    assert "🟠 medium" in out["comments"][1]["body"]
    # counts/has_findings reflect KEPT findings only.
    assert out["counts"] == {"high": 1, "medium": 1, "low": 0}
    assert out["has_findings"] is True
    # Truncated 1 here + 2 pre-dropped by the caller = 3 in the note.
    assert "3 lower-severity finding(s) dropped for the per-review cap." in out["summary"]


def test_format_review_all_below_floor_leaves_summary_artifact():
    out = _fmt([_finding("low"), _finding("low", 2)], files_reviewed=2,
               min_severity="high")
    assert out["has_findings"] is False
    assert out["comments"] == []
    assert "Nothing rose above" in out["summary"]
    assert "2 finding(s) below the min_severity floor dropped." in out["summary"]


def test_format_review_defaults_unchanged():
    # A call without the new args behaves exactly as before for findings all
    # >= low and <= 10: nothing dropped, no floor/cap notes in the summary.
    findings = [_finding("low", 1), _finding("medium", 2), _finding("high", 3)]
    out = _fmt(findings, files_reviewed=1)
    assert len(out["comments"]) == 3
    assert out["counts"] == {"high": 1, "medium": 1, "low": 1}
    assert out["has_findings"] is True
    assert "floor" not in out["summary"]
    assert "cap" not in out["summary"]

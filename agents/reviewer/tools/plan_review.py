"""Chunk a unified PR diff into reviewable units, applying the repo's
`ignore_paths`.

This is one of the few fleet tools that does real deterministic work rather than
returning a task prompt: parsing a unified diff and glob-filtering paths is
mechanical, error-prone for an LLM to do by hand, and must be reproducible. The
LLM still does the actual reviewing — this just hands it a clean, ordered work
list so it never wastes a turn re-parsing the diff or reviewing a lockfile.
"""

import fnmatch
import re

from strands import tool

# A new-file hunk header: @@ -old,cnt +new,cnt @@
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")


def _ignored(path: str, ignore_paths: list[str]) -> bool:
    """True if the path matches any ignore glob. `dist/**` matches anything under
    dist/; `*.lock` matches by basename or full path."""
    for pat in ignore_paths or []:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path.split("/")[-1], pat):
            return True
        # `dir/**` should also match `dir/a/b`; fnmatch's ** isn't recursive.
        if pat.endswith("/**") and (path == pat[:-3] or path.startswith(pat[:-2])):
            return True
    return False


def _split_files(diff: str) -> list[dict]:
    """Split a unified diff into per-file blocks with their hunks."""
    files: list[dict] = []
    cur: dict | None = None
    for line in (diff or "").splitlines():
        fm = _FILE_RE.match(line)
        if fm:
            if cur:
                if cur["_buf"]:
                    cur["hunks"].append("\n".join(cur["_buf"]))
                    cur["_buf"] = []
                files.append(cur)
            cur = {"path": fm.group(2), "hunks": [], "_buf": []}
            continue
        if cur is None:
            continue
        hm = _HUNK_RE.match(line)
        if hm:
            if cur["_buf"]:
                cur["hunks"].append("\n".join(cur["_buf"]))
            cur["_buf"] = [line]
            start = int(hm.group(1))
            cur.setdefault("hunk_starts", []).append(start)
        elif cur["_buf"]:
            cur["_buf"].append(line)
    if cur:
        if cur["_buf"]:
            cur["hunks"].append("\n".join(cur["_buf"]))
        files.append(cur)
    for f in files:
        f.pop("_buf", None)
    return files


@tool
def plan_review(diff: str, ignore_paths: list[str] = None, max_findings: int = 10) -> dict:
    """Chunk a unified PR diff into an ordered, ignore-filtered review work list.

    Call this after reading the diff (get_pull_request_diff) and BEFORE
    reviewing. Each unit is one changed file with its hunks; ignored paths are
    dropped and reported separately so you can note them in the summary.

    Args:
        diff: The unified diff text from get_pull_request_diff.
        ignore_paths: Globs never reviewed (from .pdlc-agents/review.yaml, or the
            fleet defaults). e.g. ["*.lock", "dist/**"].
        max_findings: The repo's hard cap per review — surfaced back so you keep
            the review within budget and note overflow in the summary.

    Returns:
        {
          "units": [{"path": str, "hunks": [str], "hunk_start_lines": [int]}],
          "ignored_files": [str],   # paths dropped by ignore_paths
          "file_count": int,        # reviewable files (excludes ignored)
          "max_findings": int,      # echoed budget
          "guidance": str,          # what to do next
        }
    """
    files = _split_files(diff)
    units = []
    ignored = []
    for f in files:
        if _ignored(f["path"], ignore_paths or []):
            ignored.append(f["path"])
            continue
        units.append({
            "path": f["path"],
            "hunks": f["hunks"],
            "hunk_start_lines": f.get("hunk_starts", []),
        })
    guidance = (
        f"Review these {len(units)} files in order. For each hunk, decide if there "
        "is a concrete correctness/safety/soundness defect. Every finding needs a "
        "failure scenario (inputs/state → wrong outcome) — drop any you can't "
        f"articulate one for. Keep total findings <= {max_findings}; if you drop "
        "overflow, say so in the summary. Do NOT review the ignored files."
    )
    return {
        "units": units,
        "ignored_files": ignored,
        "file_count": len(units),
        "max_findings": max_findings,
        "guidance": guidance,
    }

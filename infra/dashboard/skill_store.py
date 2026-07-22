"""Skill storage for the fleet (spec §6).

Handles upload (validation + S3 write), listing, and deletion of `SKILL.md`
packages — both raw `.md` and `.zip` (the unpack adapter). Storage is S3-backed
(SKILLS_BUCKET env) so skills persist across deploys. AgentCore runtimes are
immutable containers, so the generic base agent pulls its referenced packages
from this bucket on startup (verifying the normalized-tree sha256 recorded here) —
see agents/_base/agent.py::_sync_skills_from_s3.

Security (§6.3): .zip validation/expansion runs in an ISOLATED Lambda
(skill_unpacker.py), NOT in the admin API Lambda — the admin stages the raw zip
and invokes the unpacker, which calls upload_skill_zip here inside its own minimal
(S3-only) blast radius. The guards below are the same regardless of caller:
  - zip-slip (entries escaping root) → rejected
  - symlinks → rejected
  - oversize (per-file 5 MB, total 50 MB) → rejected
  - no top-level SKILL.md → rejected (not a valid skill package)
  - frontmatter validated: name (lowercase/hyphen, ≤64), description (required)
  - nothing is executed — scripts/ files are stored as data

The admin routes (`POST /admin/skills`, `GET /admin/skills`, `DELETE`) dispatch
here; the capability row's `skills` list references prefixes + sha256 so the
deployer can sync exactly what was uploaded.
"""

import hashlib
import io
import logging
import os
import re
import zipfile

import boto3
import yaml

logger = logging.getLogger(__name__)

SKILLS_BUCKET = os.environ.get("SKILLS_BUCKET", "")
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB per file
_MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50 MB total

# The only storage scopes a skill may live under (spec §6.1). The scope is a raw
# S3 key segment (skills/<scope>/<name>/) AND a capability-row field, so it MUST
# be allowlisted — a free-form scope could nest prefixes ("shared/x"), collide
# with the _staging/ area, or plant objects the list/teardown paths never see.
#
# NOTE: both scopes are GLOBAL namespaces keyed by skill name — "capability"
# marks a package as owned-by-whichever-capabilities-reference-it (teardown
# deletes it only when no other row references it), not as per-agent-private.
# Re-uploading a name replaces the tree for every referencing agent; an agent
# whose recorded sha256 no longer matches then DROPS the skill at startup
# (fail-closed, agents/_base/agent.py) rather than loading changed content.
VALID_SCOPES = ("shared", "capability")

# S3 object-metadata key on each package's root SKILL.md recording the
# normalized-tree sha256 at upload. list_skills() reads it back so the authoring
# UI attaches skills WITH their content hash — a skill reference persisted with
# an empty sha256 would silently skip the base agent's startup integrity check.
_SHA_METADATA_KEY = "skill-tree-sha256"

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


class SkillValidationError(Exception):
    pass


def _validate_scope(scope: str) -> str:
    """Allowlist the storage scope (see VALID_SCOPES). Raises on anything else —
    the scope becomes a raw S3 key segment and a capability-row field."""
    if scope not in VALID_SCOPES:
        raise SkillValidationError(
            f"invalid skill scope {scope!r} — must be one of {list(VALID_SCOPES)}"
        )
    return scope


def _clear_prefix(prefix: str) -> int:
    """Delete every object under ``prefix``; returns how many were deleted.
    Used by re-uploads (a package is replaced wholesale — stale leftovers would
    fail the base agent's full-prefix-tree hash check and silently drop the
    skill) and by delete_skill. Pages the listing and chunks delete_objects to
    its 1000-key cap."""
    paginator = _get_s3().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=SKILLS_BUCKET, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    for i in range(0, len(keys), 1000):
        _get_s3().delete_objects(
            Bucket=SKILLS_BUCKET,
            Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]},
        )
    return len(keys)


def normalized_tree_sha256(files: list[tuple[str, bytes]]) -> str:
    """Content hash over a skill's normalized file tree (§6.3): for each file,
    sorted by its path RELATIVE to the package root, feed ``rel + \\0 + body``.
    Independent of upload order and of the (scope/name) prefix, so the same tree
    hashes identically at upload here and at sync time in the base agent — the two
    MUST agree or every skill would fail the startup hash check and be dropped.
    ``files`` is ``[(relative_path, bytes), ...]``."""
    digest = hashlib.sha256()
    for rel, body in sorted(files, key=lambda kv: kv[0]):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()


def _validate_frontmatter(content: str) -> dict:
    """Parse and validate a SKILL.md's YAML frontmatter. Returns the metadata
    dict; raises SkillValidationError on invalid/missing fields."""
    if not content.startswith("---"):
        raise SkillValidationError("SKILL.md must begin with YAML frontmatter (---)")
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise SkillValidationError("SKILL.md frontmatter not terminated (missing closing ---)")
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"SKILL.md frontmatter is invalid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillValidationError("SKILL.md frontmatter must be a YAML mapping")
    name = meta.get("name", "")
    if not name or not _SKILL_NAME_RE.match(str(name)):
        raise SkillValidationError(
            f"SKILL.md frontmatter 'name' must be lowercase alphanumeric + hyphens, 1-64 chars (got {name!r})"
        )
    if not meta.get("description"):
        raise SkillValidationError("SKILL.md frontmatter must include a non-empty 'description'")
    return meta


def upload_skill_md(content: str, scope: str = "shared") -> dict:
    """Upload a single SKILL.md (raw markdown). Validates frontmatter, writes to
    S3, returns the skill reference ``{name, s3_prefix, sha256, scope}``. A
    re-upload replaces the whole prefix (a prior .zip upload of the same name may
    have left scripts/ files that would break the tree hash)."""
    _validate_scope(scope)
    meta = _validate_frontmatter(content)
    name = meta["name"]
    # Hash the normalized tree (here a single SKILL.md), NOT the raw markdown, so
    # it matches what the base agent recomputes from S3 on sync (§6.3).
    sha = normalized_tree_sha256([("SKILL.md", content.encode())])
    prefix = f"skills/{scope}/{name}/"
    _clear_prefix(prefix)
    _get_s3().put_object(
        Bucket=SKILLS_BUCKET,
        Key=f"{prefix}SKILL.md",
        Body=content.encode(),
        ContentType="text/markdown",
        Metadata={_SHA_METADATA_KEY: sha},
    )
    return {"name": name, "s3_prefix": prefix, "sha256": sha, "scope": scope}


def upload_skill_zip(data: bytes, scope: str = "shared") -> dict:
    """Upload a .zip skill package: validate (§6.3), expand to S3, return the
    skill reference. Raises SkillValidationError on any safety or format issue."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillValidationError(f"invalid zip file: {exc}") from exc

    # Safety checks.
    total_size = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        # Zip-slip: reject paths that escape the root.
        resolved = os.path.normpath(info.filename)
        if resolved.startswith("..") or resolved.startswith("/"):
            raise SkillValidationError(f"zip-slip detected: {info.filename}")
        # Symlinks: zipfile external_attr can encode them (Unix high byte).
        if (info.external_attr >> 28) == 0xA:
            raise SkillValidationError(f"symlink in zip: {info.filename}")
        if info.file_size > _MAX_FILE_SIZE:
            raise SkillValidationError(
                f"{info.filename} exceeds per-file limit ({info.file_size} > {_MAX_FILE_SIZE})"
            )
        total_size += info.file_size
        if total_size > _MAX_TOTAL_SIZE:
            raise SkillValidationError(f"zip total size exceeds limit ({_MAX_TOTAL_SIZE})")

    # Require a SKILL.md at the package root. Tools commonly zip a skill WITH a
    # wrapping directory (``my-skill/SKILL.md``) — accept that shape too, but
    # then treat that directory as the package root and STRIP it from every
    # stored path: AgentSkills expects SKILL.md at the package directory's root,
    # so storing the wrapper one level deep would make the skill invisible at
    # runtime. A SKILL.md nested deeper than one level is not a package root.
    names = zf.namelist()
    skill_md_path = next(
        (n for n in names if n.rstrip("/") == "SKILL.md" or (n.endswith("/SKILL.md") and n.count("/") == 1)),
        None,
    )
    if not skill_md_path:
        raise SkillValidationError("zip must contain a top-level SKILL.md")
    root = skill_md_path[: -len("SKILL.md")]  # "" or "my-skill/"
    skill_md_content = zf.read(skill_md_path).decode("utf-8", errors="replace")
    meta = _validate_frontmatter(skill_md_content)
    name = meta["name"]

    _validate_scope(scope)
    prefix = f"skills/{scope}/{name}/"

    # Collect every file's root-relative path first — entries OUTSIDE the wrapper
    # root are rejected (they'd otherwise be stored under a path the runtime
    # never looks at, or clash with another package's tree).
    entries: list[tuple[str, bytes]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        norm = os.path.normpath(info.filename)
        norm_root = os.path.normpath(root) + os.sep if root else ""
        if norm_root and not norm.startswith(norm_root):
            raise SkillValidationError(
                f"zip entry {info.filename!r} is outside the package root {root!r}"
            )
        rel = norm[len(norm_root):] if norm_root else norm
        if not rel:
            continue
        entries.append((rel, zf.read(info.filename)))

    # Replace the prefix WHOLESALE: a re-upload with a smaller file set must not
    # leave stale objects behind — the base agent hashes the full prefix tree at
    # sync time, so leftovers would fail every startup check and drop the skill.
    _clear_prefix(prefix)

    # Write every file to S3 under the prefix, hashing the NORMALIZED TREE (§6.3)
    # — the exact set of objects under the prefix, keyed by their root-relative
    # path. This is what the base agent recomputes from S3 on sync, so the two
    # agree by construction (a raw sha256(zip_bytes) would not, since the agent
    # never sees the original zip).
    sha = normalized_tree_sha256(entries)
    for rel, body in entries:
        extra = {"Metadata": {_SHA_METADATA_KEY: sha}} if rel == "SKILL.md" else {}
        _get_s3().put_object(
            Bucket=SKILLS_BUCKET,
            Key=f"{prefix}{rel}",
            Body=body,
            **extra,
        )
    return {"name": name, "s3_prefix": prefix, "sha256": sha, "scope": scope}


def _list_common_prefixes(prefix: str) -> list[str]:
    """Return every immediate sub-prefix of ``prefix`` (Delimiter='/'), paging
    through the full result set — list_objects_v2 caps a single response at 1000
    CommonPrefixes, so a bucket with >1000 scopes or >1000 skills in a scope would
    silently truncate without the paginator."""
    prefixes: list[str] = []
    paginator = _get_s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SKILLS_BUCKET, Prefix=prefix, Delimiter="/"):
        prefixes.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return prefixes


def _prefix_sha256(prefix: str) -> str:
    """The package's recorded normalized-tree sha256, read back from the root
    SKILL.md's object metadata (written at upload). Falls back to RECOMPUTING the
    hash over the prefix tree for packages uploaded before the metadata existed —
    never returns empty for a real package, because a skill reference persisted
    with sha256="" would silently skip the base agent's startup integrity check.
    The recomputed hash is written BACK to the metadata (best-effort) so a legacy
    package pays the full-tree download once, not on every listing — list_skills
    sits on the dashboard's polling path."""
    s3 = _get_s3()
    root_key = f"{prefix}SKILL.md"
    try:
        head = s3.head_object(Bucket=SKILLS_BUCKET, Key=root_key)
        recorded = (head.get("Metadata") or {}).get(_SHA_METADATA_KEY, "")
        if recorded:
            return recorded
    except Exception:  # noqa: BLE001 — missing root doc; recompute below
        pass
    files: list[tuple[str, bytes]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SKILLS_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel:
                continue
            body = s3.get_object(Bucket=SKILLS_BUCKET, Key=obj["Key"])["Body"].read()
            files.append((rel, body))
    if not files:
        return ""
    sha = normalized_tree_sha256(files)
    try:
        # Self-copy to persist the metadata; a failure just means the next
        # listing recomputes again (correctness unaffected).
        s3.copy_object(
            Bucket=SKILLS_BUCKET,
            Key=root_key,
            CopySource={"Bucket": SKILLS_BUCKET, "Key": root_key},
            Metadata={_SHA_METADATA_KEY: sha},
            MetadataDirective="REPLACE",
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not backfill sha metadata for %s", prefix)
    return sha


def list_skills() -> list[dict]:
    """List all uploaded skill packages (by unique prefix). Returns
    ``[{name, s3_prefix, sha256, scope}]``. The sha256 comes from the root
    SKILL.md's object metadata (one HEAD per package) — it MUST be present in the
    listing because the authoring UI attaches skills from it verbatim, and a
    reference persisted without a hash would disable the base agent's startup
    integrity verification for that skill."""
    if not SKILLS_BUCKET:
        return []
    # Top-level prefixes are skills/<scope>/ — we need to go one level deeper.
    skills: list[dict] = []
    for scope_prefix in _list_common_prefixes("skills/"):
        scope = scope_prefix.rstrip("/").split("/")[-1]
        for skill_prefix in _list_common_prefixes(scope_prefix):
            name = skill_prefix.rstrip("/").split("/")[-1]
            skills.append({
                "name": name,
                "s3_prefix": skill_prefix,
                "sha256": _prefix_sha256(skill_prefix),
                "scope": scope,
            })
    return skills


def delete_skill(scope: str, name: str) -> bool:
    """Delete all objects under a skill's prefix. Returns True if anything was
    deleted; False if the prefix was empty/nonexistent."""
    return _clear_prefix(f"skills/{scope}/{name}/") > 0

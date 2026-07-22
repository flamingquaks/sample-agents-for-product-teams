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

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


class SkillValidationError(Exception):
    pass


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
    S3, returns the skill reference ``{name, s3_prefix, sha256, scope}``."""
    meta = _validate_frontmatter(content)
    name = meta["name"]
    # Hash the normalized tree (here a single SKILL.md), NOT the raw markdown, so
    # it matches what the base agent recomputes from S3 on sync (§6.3).
    sha = normalized_tree_sha256([("SKILL.md", content.encode())])
    prefix = f"skills/{scope}/{name}/"
    _get_s3().put_object(
        Bucket=SKILLS_BUCKET,
        Key=f"{prefix}SKILL.md",
        Body=content.encode(),
        ContentType="text/markdown",
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

    # Require a top-level SKILL.md.
    names = zf.namelist()
    skill_md_path = next(
        (n for n in names if n.rstrip("/") == "SKILL.md" or (n.endswith("/SKILL.md") and n.count("/") == 1)),
        None,
    )
    if not skill_md_path:
        raise SkillValidationError("zip must contain a top-level SKILL.md")
    skill_md_content = zf.read(skill_md_path).decode("utf-8", errors="replace")
    meta = _validate_frontmatter(skill_md_content)
    name = meta["name"]

    prefix = f"skills/{scope}/{name}/"

    # Write every file to S3 under the prefix, collecting (rel_path, bytes) so the
    # recorded hash is over the NORMALIZED TREE (§6.3) — the exact set of objects
    # under the prefix, keyed by their path relative to it. This is what the base
    # agent recomputes from S3 on sync, so the two agree by construction (a raw
    # sha256(zip_bytes) would not, since the agent never sees the original zip).
    written: list[tuple[str, bytes]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = os.path.normpath(info.filename)
        body = zf.read(info.filename)
        _get_s3().put_object(
            Bucket=SKILLS_BUCKET,
            Key=f"{prefix}{rel}",
            Body=body,
        )
        written.append((rel, body))
    sha = normalized_tree_sha256(written)
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


def list_skills() -> list[dict]:
    """List all uploaded skill packages (by unique prefix). Returns
    ``[{name, s3_prefix, scope}]`` — lightweight, no content fetch."""
    if not SKILLS_BUCKET:
        return []
    # Top-level prefixes are skills/<scope>/ — we need to go one level deeper.
    skills: list[dict] = []
    for scope_prefix in _list_common_prefixes("skills/"):
        scope = scope_prefix.rstrip("/").split("/")[-1]
        for skill_prefix in _list_common_prefixes(scope_prefix):
            name = skill_prefix.rstrip("/").split("/")[-1]
            skills.append({"name": name, "s3_prefix": skill_prefix, "scope": scope})
    return skills


def delete_skill(scope: str, name: str) -> bool:
    """Delete all objects under a skill's prefix. Returns True if anything was
    deleted; False if the prefix was empty/nonexistent."""
    prefix = f"skills/{scope}/{name}/"
    paginator = _get_s3().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=SKILLS_BUCKET, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    if not keys:
        return False
    # delete_objects accepts at most 1000 keys per call — chunk it.
    for i in range(0, len(keys), 1000):
        _get_s3().delete_objects(
            Bucket=SKILLS_BUCKET,
            Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]},
        )
    return True

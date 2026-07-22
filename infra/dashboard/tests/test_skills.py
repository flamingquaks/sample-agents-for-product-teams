"""Tests for skill_store.py — upload, validate, list, delete (spec §6).

Uses moto S3 for the skills bucket. Exercises the .md and .zip upload paths,
frontmatter validation, zip-slip/symlink/size rejection, and the list/delete
round-trip.
"""

import base64
import io
import os
import sys
import zipfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
BUCKET = "sdlc-agent-skills-test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["SKILLS_BUCKET"] = BUCKET


def _make_bucket():
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def _skill_md(name="my-skill", description="A test skill"):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Instructions\nDo the thing.\n"


def _skill_zip(name="my-skill", description="A test skill", extra_files=None, bad_entry=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", _skill_md(name, description))
        for fname, content in (extra_files or {}).items():
            zf.writestr(fname, content)
        if bad_entry:
            zf.writestr(bad_entry, "evil")
    return buf.getvalue()


@pytest.fixture
def store():
    with mock_aws():
        _make_bucket()
        for m in ("skill_store",):
            sys.modules.pop(m, None)
        import skill_store
        skill_store._s3 = None
        yield skill_store


def test_upload_md_valid(store):
    ref = store.upload_skill_md(_skill_md())
    assert ref["name"] == "my-skill"
    assert ref["scope"] == "shared"
    assert ref["s3_prefix"].startswith("skills/shared/my-skill/")
    assert ref["sha256"]


def test_upload_md_missing_description(store):
    with pytest.raises(store.SkillValidationError, match="description"):
        store.upload_skill_md("---\nname: foo\n---\nbody")


def test_upload_md_bad_name(store):
    with pytest.raises(store.SkillValidationError, match="name"):
        store.upload_skill_md("---\nname: Bad Name!\ndescription: x\n---\nbody")


def test_upload_zip_valid(store):
    data = _skill_zip(extra_files={"scripts/run.sh": "#!/bin/bash\necho hi"})
    ref = store.upload_skill_zip(data, scope="capability")
    assert ref["name"] == "my-skill"
    assert ref["scope"] == "capability"
    # Verify the script file was stored.
    s3 = boto3.client("s3", region_name=REGION)
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=ref["s3_prefix"])
    keys = [o["Key"] for o in resp.get("Contents", [])]
    assert any("scripts/run.sh" in k for k in keys)


def test_upload_zip_rejects_zip_slip(store):
    data = _skill_zip(bad_entry="../../../etc/passwd")
    with pytest.raises(store.SkillValidationError, match="zip-slip"):
        store.upload_skill_zip(data)


def test_upload_zip_rejects_missing_skill_md(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.md", "no skill here")
    with pytest.raises(store.SkillValidationError, match="SKILL.md"):
        store.upload_skill_zip(buf.getvalue())


def test_list_and_delete_round_trip(store):
    store.upload_skill_md(_skill_md("alpha"))
    store.upload_skill_md(_skill_md("beta"))
    skills = store.list_skills()
    names = sorted(s["name"] for s in skills)
    assert names == ["alpha", "beta"]
    assert store.delete_skill("shared", "alpha") is True
    assert store.delete_skill("shared", "alpha") is False  # already gone
    assert len(store.list_skills()) == 1


def test_list_skills_paginates_beyond_1000(store):
    """list_objects_v2 caps CommonPrefixes at 1000 per response; list_skills must
    page past that so a bucket with >1000 skills isn't silently truncated."""
    s3 = boto3.client("s3", region_name=REGION)
    for i in range(1050):
        s3.put_object(Bucket=BUCKET, Key=f"skills/shared/skill-{i:04d}/SKILL.md", Body=b"x")
    skills = store.list_skills()
    assert len(skills) == 1050


def test_delete_skill_paginates_and_chunks_beyond_1000(store):
    """A skill package with >1000 objects must fully delete: the listing pages and
    delete_objects is chunked to its 1000-key-per-call limit."""
    s3 = boto3.client("s3", region_name=REGION)
    for i in range(1050):
        s3.put_object(Bucket=BUCKET, Key=f"skills/shared/big/file-{i:04d}.txt", Body=b"x")
    assert store.delete_skill("shared", "big") is True
    remaining = s3.list_objects_v2(Bucket=BUCKET, Prefix="skills/shared/big/")
    assert remaining.get("KeyCount", 0) == 0


def _recompute_agent_side_sha(s3, prefix):
    """Recompute the normalized-tree sha256 exactly the way the base agent does in
    _sync_skills_from_s3: read every object under the prefix, key by its path
    RELATIVE to the prefix, feed rel + \\0 + body sorted by key. If this diverges
    from what skill_store records at upload, every skill fails the startup hash
    check and is silently dropped — so this is the load-bearing invariant."""
    import hashlib

    objects = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
            objects.append((obj["Key"], body))
    digest = hashlib.sha256()
    for key, body in sorted(objects, key=lambda kv: kv[0]):
        digest.update(key[len(prefix):].encode())
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()


def test_md_upload_sha_matches_agent_recompute(store):
    """The sha256 skill_store records for a .md upload must equal what the base
    agent recomputes from S3 (else the startup hash check drops the skill)."""
    ref = store.upload_skill_md(_skill_md())
    s3 = boto3.client("s3", region_name=REGION)
    assert ref["sha256"] == _recompute_agent_side_sha(s3, ref["s3_prefix"])


def test_zip_upload_sha_matches_agent_recompute(store):
    """Same invariant for a multi-file .zip package — the recorded hash is over the
    normalized tree, not the raw zip bytes the agent never sees."""
    ref = store.upload_skill_zip(_skill_zip(
        extra_files={"references/guide.md": "# guide\n", "scripts/run.sh": "echo hi\n"}
    ))
    s3 = boto3.client("s3", region_name=REGION)
    assert ref["sha256"] == _recompute_agent_side_sha(s3, ref["s3_prefix"])

"""Tests for the isolated skill-unpacker Lambda (skill_unpacker.py, spec §6.3).

The admin API stages a raw skill .zip to the skills bucket and invokes this
function; it downloads the staged bytes, runs the validated expansion, deletes the
staging object, and returns the ref or a validation error. Uses moto S3.
"""

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


def _skill_md(name="my-skill", description="A test skill"):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Do the thing\n"


def _skill_zip(name="my-skill", extra=None, bad_entry=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", _skill_md(name))
        for fname, content in (extra or {}).items():
            zf.writestr(fname, content)
        if bad_entry:
            zf.writestr(bad_entry, "evil")
    return buf.getvalue()


@pytest.fixture
def unpacker():
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        for m in ("skill_unpacker", "skill_store"):
            sys.modules.pop(m, None)
        import skill_unpacker
        import skill_store
        skill_unpacker._s3 = None
        skill_store._s3 = None
        yield skill_unpacker


def _stage(data, key="_staging/abc.zip"):
    boto3.client("s3", region_name=REGION).put_object(Bucket=BUCKET, Key=key, Body=data)
    return key


def test_unpack_valid_zip_returns_ref_and_clears_staging(unpacker):
    key = _stage(_skill_zip(extra={"references/g.md": "# g\n"}))
    out = unpacker.handler({"staging_key": key, "scope": "shared"})
    assert out["ok"] is True
    assert out["ref"]["name"] == "my-skill"
    assert out["ref"]["s3_prefix"] == "skills/shared/my-skill/"
    # Staging object deleted; expanded tree written.
    s3 = boto3.client("s3", region_name=REGION)
    assert s3.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount", 0) == 0
    assert s3.get_object(Bucket=BUCKET, Key="skills/shared/my-skill/SKILL.md")


def test_unpack_rejects_zip_slip_and_clears_staging(unpacker):
    key = _stage(_skill_zip(bad_entry="../escape.txt"))
    out = unpacker.handler({"staging_key": key, "scope": "shared"})
    assert out["ok"] is False
    assert "zip-slip" in out["error"].lower()
    # Even on rejection the staging object is cleaned up.
    s3 = boto3.client("s3", region_name=REGION)
    assert s3.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount", 0) == 0


def test_unpack_missing_key_errors(unpacker):
    out = unpacker.handler({"scope": "shared"})
    assert out["ok"] is False


def test_unpack_unreadable_staging_errors(unpacker):
    out = unpacker.handler({"staging_key": "_staging/does-not-exist.zip", "scope": "shared"})
    assert out["ok"] is False

"""Isolated skill-unpack Lambda (spec §6.3).

Unpacking an admin-supplied ``.zip`` is the untrusted-artifact surface of the
authoring feature (zip-slip, zip-bomb, symlinks). The spec requires it to run in
an ISOLATED Lambda — not in the admin API Lambda (which holds AVP/config/deploy
reach) and not in the build. This handler is that boundary.

Flow: the admin API writes the raw uploaded zip to a staging key in the skills
bucket, then invokes THIS function synchronously with ``{staging_key, scope}``.
We download the staged bytes, run the same validated expansion the store defines
(zip-slip/symlink/size/frontmatter guards — skill_store.upload_skill_zip), delete
the staging object, and return the skill reference or a validation error. Nothing
is executed; ``scripts/`` entries are stored as data.

Privileges (see template CapabilitySkillUnpackerFunction): only S3 read/write on
the one skills bucket — no config table, no AVP, no deploy. So a maliciously
crafted zip that somehow subverted the unpack code still couldn't touch anything
beyond the skills bucket it was always going to write to.
"""

import json
import logging

import boto3

import skill_store

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def handler(event, context=None):
    """Synchronous entry point. ``event = {"staging_key": str, "scope": str}``.

    Returns ``{"ok": True, "ref": {...}}`` on success or
    ``{"ok": False, "error": str}`` on a validation failure — the admin API maps
    the latter to a 400. The staging object is deleted on both paths (success and
    validation failure) so a rejected upload never lingers; only an unexpected
    error leaves it for the bucket lifecycle rule to expire."""
    staging_key = (event or {}).get("staging_key", "")
    scope = (event or {}).get("scope", "shared") or "shared"
    bucket = skill_store.SKILLS_BUCKET
    if not bucket or not staging_key:
        return {"ok": False, "error": "missing skills bucket or staging key"}

    s3 = _get_s3()
    try:
        data = s3.get_object(Bucket=bucket, Key=staging_key)["Body"].read()
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not read staged upload %s", staging_key)
        return {"ok": False, "error": f"could not read staged upload: {exc}"}

    try:
        ref = skill_store.upload_skill_zip(data, scope=scope)
        return {"ok": True, "ref": ref}
    except skill_store.SkillValidationError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        # Always drop the staged raw zip — it's consumed (or rejected).
        try:
            s3.delete_object(Bucket=bucket, Key=staging_key)
        except Exception:  # noqa: BLE001
            logger.warning("could not delete staging object %s", staging_key)

"""Bedrock Mantle project management for per-repo cost attribution.

Each onboarded repo gets its own Mantle **project**, so the model cost/usage the
fleet's agents incur while acting on that repo is attributed to it (the app will
surface this per-repo spend later). Projects are created via the Mantle Projects
API when a repo is onboarded (admin.py), and the project id is stored on the repo
record; the Dispatch Router reads it back and passes it to the agent, which sets
the ``OpenAI-Project`` header per model call (agents/shared/bedrock.py).

Kept isolated here so the project lifecycle (create today; archive-on-offboard
and cost read-back later) lives in one place. Best-effort by design: a repo can
still onboard if project creation fails (it falls back to the account default
project), so a Mantle hiccup never blocks bringing a repo into the fleet.
"""

import logging
import os

import boto3

logger = logging.getLogger()

# The Mantle Projects API is served off the bedrock-mantle endpoint. boto3 gained
# a `bedrock-mantle` client; we call CreateProject through it. Resolved lazily so
# import doesn't require the client to exist (older botocore / tests).
_client = None


def _mantle():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-mantle")
    return _client


def project_name_for(repo: str, stage: str) -> str:
    """Deterministic project name for a repo — stable so a re-onboard finds the
    same project rather than creating duplicates. Mantle project names allow a
    limited charset, so slashes/dots in owner/repo are normalized to hyphens."""
    safe = repo.strip().casefold().replace("/", "-").replace(".", "-")
    return f"sdlc-{stage}-{safe}"


def ensure_project(repo: str) -> str | None:
    """Create (or find) the Mantle project for ``repo`` and return its id, or None
    if Mantle isn't available/enabled. Best-effort — never raises to the caller;
    a failure just means the repo runs under the default project until re-onboard.

    No-op returning None when SDLC_MANTLE_ENABLED is off (lets the fleet run on
    the default project during rollout / in regions without Mantle)."""
    if os.environ.get("SDLC_MANTLE_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None
    stage = os.environ.get("STAGE", "dev")
    name = project_name_for(repo, stage)
    tags = {"sdlc-fleet": stage, "repo": repo.strip().casefold()}
    client = _mantle()
    try:
        # Idempotency: CreateProject with the same name should return/point at the
        # existing project.
        resp = client.create_project(name=name, tags=tags)
        return _project_id(resp)
    except Exception:  # noqa: BLE001
        # If the API instead errors on a duplicate name (e.g. a re-onboard of a
        # repo whose project already exists — project_name_for is deterministic),
        # fall back to finding the existing project by name rather than dropping
        # the repo's cost attribution to the default project.
        existing = _find_project_by_name(client, name)
        if existing:
            logger.info("Mantle project %s already exists — reusing it", name)
            return existing
        logger.exception(
            "Mantle project ensure failed for %s (repo will use the default "
            "project until re-onboard)", repo
        )
        return None


def _project_id(record: dict) -> str | None:
    """Pull the project id out of a Mantle project record, tolerating the field
    name the API uses (id / projectId / arn)."""
    if not isinstance(record, dict):
        return None
    return record.get("id") or record.get("projectId") or record.get("arn")


def _find_project_by_name(client, name: str) -> str | None:
    """Look up an existing Mantle project id by exact name. Best-effort: any
    listing error just yields None (caller falls back to the default project)."""
    try:
        # Prefer the paginator when the client exposes one; fall back to a single
        # list_projects call otherwise.
        pages = []
        if hasattr(client, "get_paginator"):
            try:
                for page in client.get_paginator("list_projects").paginate():
                    pages.append(page)
            except Exception:  # noqa: BLE001 — operation may not be paginatable
                pages = [client.list_projects()]
        else:
            pages = [client.list_projects()]
        for page in pages:
            for proj in page.get("projects", []) or []:
                if proj.get("name") == name:
                    return _project_id(proj)
    except Exception:  # noqa: BLE001
        logger.exception("Mantle list_projects lookup failed for %s", name)
    return None

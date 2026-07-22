"""Weekly security rebuild of every active capability's container.

A scheduled EventBridge rule invokes this Lambda on a weekly cadence. For each
capability that is currently ``active``, it starts the SAME shared build pipeline
an onboard uses (a fresh IMAGE_TAG), which rebuilds the agent image against the
current base image + dependencies — picking up OS/library security patches. Build
completion flows through the normal path (capability_deployer), which updates the
runtime to the fresh image and re-marks the capability active.

Safety: this only STARTS builds (codebuild:StartBuild on the one project). It does
not touch runtimes. A build or runtime-update that fails leaves the capability
``failed`` (via the deployer) with the current runtime still serving the previous
image — a failed security rebuild never takes an agent down. Only ``active``
capabilities are rebuilt; ones mid-onboard (pending/building) or already failed are
skipped so the weekly job doesn't disturb an in-flight or broken deploy.

Approval gate (spec §7.5): a capability whose ``review_status`` is
``pending_review`` is ALSO skipped. An edit that adds a novel dependency/skill to
an already-active, approved agent re-parks it pending_review but leaves it
``active`` (its old runtime keeps serving, correctly). The build itself reads the
LIVE capability row (buildspec → gen_requirements.py), so rebuilding a
pending_review row would pip-install the unapproved dependency in the build
container — bypassing the second-admin gate the onboard path enforces. Skipping
pending_review here closes that path; the rebuild happens after approval, which
starts its own build.
"""

import logging
import os

import boto3

import config_store

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event=None, context=None):
    """EventBridge scheduled entry point. Starts a rebuild for every active
    capability; returns a summary. Never raises for a single capability's failure
    — it records the count and moves on, so one bad agent can't skip the rest."""
    project = os.environ.get("CAPABILITY_BUILD_PROJECT")
    if not project:
        logger.warning("CAPABILITY_BUILD_PROJECT unset — no build pipeline; nothing to rebuild")
        return {"rebuilt": 0, "skipped": 0, "reason": "no build project"}

    codebuild = boto3.client("codebuild")
    # A monotonic-ish tag from the scheduled event time; Lambda has no stable
    # per-invocation counter and Math.random-style entropy isn't needed — each
    # weekly run gets a distinct tag from the event's time field, falling back to
    # the request id so two agents in one run still share a coherent batch tag.
    stamp = ""
    if isinstance(event, dict):
        stamp = str(event.get("time", "")).replace(":", "").replace("-", "").replace("T", "").rstrip("Z")
    if not stamp and context is not None:
        stamp = getattr(context, "aws_request_id", "")[:12]
    tag_base = f"weekly-{stamp or 'rebuild'}"

    rebuilt, skipped = 0, 0
    for cap in config_store.list_capabilities():
        agent_id = cap.get("agent_id", "")
        if cap.get("status") != config_store.CAP_ACTIVE:
            skipped += 1
            continue
        # Never rebuild an agent parked for second-admin approval: the build
        # reads the live row and would pip-install its unapproved deps (§7.5).
        if cap.get("review_status") == "pending_review":
            logger.info("skipping %s — pending_review (unapproved deps/skills)", agent_id)
            skipped += 1
            continue
        image_tag = f"{tag_base}-{agent_id}"
        try:
            codebuild.start_build(
                projectName=project,
                environmentVariablesOverride=[
                    {"name": "AGENT_NAME", "value": agent_id, "type": "PLAINTEXT"},
                    {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
                ],
            )
            config_store.set_capability_status(
                agent_id, config_store.CAP_BUILDING, detail=f"weekly security rebuild ({image_tag})"
            )
            rebuilt += 1
            logger.info("started weekly rebuild for %s (%s)", agent_id, image_tag)
        except Exception:  # noqa: BLE001
            logger.exception("failed to start weekly rebuild for %s", agent_id)
            skipped += 1

    logger.info("weekly rebuild: started %d, skipped %d", rebuilt, skipped)
    return {"rebuilt": rebuilt, "skipped": skipped}

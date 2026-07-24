"""Generic base agent for config-driven (custom) capabilities (spec §4).

Reads its identity, prompt, and skill configuration from the runtime environment
rather than hardcoded imports — so a new agent is a capability row + this base
image, no code checkin. The tool set comes from the Gateway (policy-filtered per
agent-id by the Cedar engine, §3.5); skills are loaded natively via AgentSkills.

This file is the ONLY runtime-coupled piece; it sits behind a thin RUNTIME seam
so a future alternative (Codex / Kiro / Claude Code) can be introduced by
replacing this agent.py without touching the schema, deployer, or UI.
"""

import hashlib
import json
import logging
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from shared import durable
from shared.assignment import complete_assignment, extract_usage, fail_assignment
from shared.bedrock import build_model
from shared.dispatch_context import slack_dispatch_block
from shared.tools import gateway, workspace
from strands import Agent

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

AGENT_ID = os.environ.get("AGENT_ID", "custom-agent")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant.")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/app/skills")
# Skill delivery (§6.2): AgentCore runtimes are immutable, so the deployer can't
# write into the container. Instead it injects SKILLS_BUCKET + SKILLS_MANIFEST
# (a JSON list of {name, s3_prefix, sha256}), and we pull each package from S3
# into SKILLS_DIR here on startup, verifying the content hash.
SKILLS_BUCKET = os.environ.get("SKILLS_BUCKET", "")
SKILLS_MANIFEST = os.environ.get("SKILLS_MANIFEST", "")

GATEWAY_MCP_URL = os.environ.get("GATEWAY_MCP_URL")
if not GATEWAY_MCP_URL:
    logger.critical("GATEWAY_MCP_URL is required — this agent routes ALL tool calls through the Gateway")
    sys.exit(1)


def _sync_skills_from_s3() -> None:
    """Download the manifest's skill packages from S3 into SKILLS_DIR, verifying
    each package's content hash against the manifest (§6.2/§6.3). Best-effort: any
    per-skill failure (missing bucket, hash mismatch, S3 error) is logged and the
    skill skipped so the agent still starts — degraded (prompt-only for that
    skill) rather than failing to boot. Idempotent across restarts.

    The sha256 is computed over the sorted (key, bytes) of every object under the
    package prefix — the same normalization skill_store records at upload — so a
    tampered or partially-synced package is detected and dropped."""
    if not (SKILLS_BUCKET and SKILLS_MANIFEST):
        return
    try:
        manifest = json.loads(SKILLS_MANIFEST)
    except (json.JSONDecodeError, TypeError):
        logger.warning("SKILLS_MANIFEST is not valid JSON; no skills synced")
        return
    import boto3

    s3 = boto3.client("s3")
    dest_root = Path(SKILLS_DIR)
    dest_root.mkdir(parents=True, exist_ok=True)
    for entry in manifest:
        name = entry.get("name", "")
        prefix = entry.get("s3_prefix", "")
        expected = entry.get("sha256", "")
        if not name or not prefix:
            continue
        if not expected:
            # No recorded hash means the integrity check below can't run — treat
            # that as a failure, not a pass (§6.3). The admin API requires a
            # sha256 on every attached skill, so an empty one here indicates a
            # row written outside that boundary.
            logger.warning("skill %s: manifest entry has no sha256 — refusing unverifiable package", name)
            continue
        try:
            objects: list[tuple[str, bytes]] = []
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=SKILLS_BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Skip a zero-byte "folder marker" object at the prefix
                    # itself (rel would be "") — the upload-side hash never
                    # includes it, so hashing it here would make every skill
                    # fail the check if e.g. the S3 console created one.
                    if key == prefix:
                        continue
                    body = s3.get_object(Bucket=SKILLS_BUCKET, Key=key)["Body"].read()
                    objects.append((key, body))
            if not objects:
                logger.warning("skill %s: nothing under s3://%s/%s — skipping", name, SKILLS_BUCKET, prefix)
                continue
            # Verify the normalized content hash before writing anything to disk.
            digest = hashlib.sha256()
            for key, body in sorted(objects, key=lambda kv: kv[0]):
                rel = key[len(prefix):]
                digest.update(rel.encode())
                digest.update(b"\0")
                digest.update(body)
            if expected and digest.hexdigest() != expected:
                logger.warning("skill %s: sha256 mismatch (expected %s) — skipping", name, expected)
                continue
            skill_dir = dest_root / name
            for key, body in objects:
                rel = key[len(prefix):]
                if not rel:
                    continue
                target = skill_dir / rel
                # Defense in depth against a crafted key escaping the skill dir —
                # the upload adapter already rejects zip-slip, but this is the
                # write boundary so re-check.
                resolved = target.resolve()
                if not str(resolved).startswith(str(skill_dir.resolve()) + os.sep):
                    logger.warning("skill %s: entry %r escapes skill dir — skipping entry", name, rel)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            logger.info("skill %s synced to %s", name, skill_dir)
        except Exception:
            logger.exception("skill %s: sync failed — skipping", name)


def _load_skills_plugin():
    """Wire AgentSkills from the resolved SKILLS_DIR (§6). Returns None if no
    skills are present or the plugin can't be loaded (degraded: agent runs
    prompt-only without skills rather than failing to start)."""
    skills_path = Path(SKILLS_DIR)
    if not skills_path.is_dir():
        return None
    skill_dirs = [d for d in skills_path.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    if not skill_dirs:
        return None
    try:
        from strands_agents_tools.plugins import AgentSkills
        return AgentSkills(skills=[str(d) for d in skill_dirs])
    except ImportError:
        logger.warning("strands AgentSkills plugin not available; skills will not load")
        return None


# Pull skill packages once at import (cold start), before the first invoke. A
# failure here never blocks boot — _sync_skills_from_s3 swallows per-skill errors.
_sync_skills_from_s3()

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    user_input = ""
    source_context = {}
    assignment_id = ""
    source = ""
    resume = None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {"prompt": payload}
    if isinstance(payload, dict):
        try:
            body = json.loads(payload.get("body", "{}")) if isinstance(payload.get("body"), str) else payload
        except (json.JSONDecodeError, TypeError):
            body = payload
        # The router sends the text as "prompt" (invoke_agent); "instruction"/
        # "body" are kept as fallbacks for direct/manual invocations.
        user_input = (
            body.get("prompt", "") or body.get("instruction", "") or body.get("body", "")
        )
        source_context = body.get("source_context", {})
        assignment_id = body.get("assignment_id", "")
        source = body.get("source", "")
        # Resume dispatch (durable-repo-work spec): the router re-invokes the
        # runtime with the saved interrupt id + the human's reply; the S3
        # session restores the conversation, the snapshot restores the tree.
        resume = body.get("resume") if isinstance(body.get("resume"), dict) else None

    if not user_input and not resume:
        logger.error("no instruction in payload")
        return {"statusCode": 400, "body": "no instruction"}

    dispatch_repo = source_context.get("repo", "")

    with ExitStack() as stack:
        gw = stack.enter_context(
            gateway.build_gateway_client(agent=AGENT_ID)
        )
        all_tools = list(gw.list_tools_sync())

        plugins = []
        skills_plugin = _load_skills_plugin()
        if skills_plugin:
            plugins.append(skills_plugin)

        prompt = SYSTEM_PROMPT
        if source == "slack":
            # The codebase bridge for chat dispatches: which repos this channel
            # is approved for and when to reach for them.
            prompt += slack_dispatch_block(source_context)
        elif dispatch_repo:
            prompt += f"\n\nCurrent Dispatch:\n- Repository: {dispatch_repo}\n"

        # Durable session + workspace + ask_user (durable-repo-work spec):
        # binds the dispatch identity for credential scoping and only offers
        # what the runtime actually supports (git+vendor / session bucket).
        session, durable_tools, hooks, durable_prompt = durable.durable_kit(
            assignment_id,
            agent_id=AGENT_ID,
            origin=dispatch_repo,
            source=source,
            source_context=source_context,
        )
        all_tools.extend(durable_tools)
        prompt += durable_prompt

        agent = Agent(
            model=build_model(),
            system_prompt=prompt,
            tools=all_tools,
            plugins=plugins,
            hooks=hooks,
            session_manager=session,
        )

        try:
            if resume:
                # Restore the tree at the recorded shas, then feed the reply.
                workspace.restore(resume.get("workspace_snapshot") or [])
                durable.mark_resumed(assignment_id)
                result = agent(
                    durable.resume_payload(
                        str(resume.get("interrupt_id", "")),
                        str(resume.get("response", "")),
                    )
                )
            else:
                result = agent(user_input)

            pause = durable.handle_agent_result(
                result, assignment_id, workspace=workspace
            )
            if pause:
                # Paused cleanly (workspace pushed, row flipped awaiting_input).
                # The notifier posts the question to the origin thread.
                return {"statusCode": 200, "body": f"PAUSED: {pause['question']}"}

            output = str(result)
            if assignment_id:
                complete_assignment(
                    assignment_id,
                    result_summary=output[:3500],
                    token_usage=extract_usage(result),
                )
        except Exception as exc:
            logger.exception("agent run failed")
            if assignment_id:
                fail_assignment(assignment_id, error=str(exc))
            return {"statusCode": 500, "body": f"agent error: {exc}"}

    body = output if len(output) <= 5000 else output[:5000] + "\n\n…[output truncated]"
    return {"statusCode": 200, "body": body}


if __name__ == "__main__":
    app.run()

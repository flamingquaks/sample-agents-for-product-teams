"""Discord Interactions Endpoint Lambda.

Receives Discord slash commands and forwards recognized agent commands to the
Dispatch Router. Sits behind API Gateway at POST /discord/interactions.

Slash commands only (v1). Discord @mention events require a long-running
Gateway WebSocket listener, which is the wrong shape for Lambda -- see
docs/03-design-agent-fleet.md section 4.4 for the roadmap item.

Interaction types handled:
1. PING (type 1) -- Discord sends this on every Interactions URL save; we must
   respond with {"type": 1} within 3 seconds (signature already verified).
2. APPLICATION_COMMAND (type 2) -- A user invoked /workitems, /docwriter, etc.
   We acknowledge synchronously with {"type": 5} (deferred channel message,
   shows "Bot is thinking...") then async-invoke the Dispatch Router. The agent
   posts a follow-up via POST /webhooks/{application_id}/{interaction_token}.

Signature scheme: Ed25519. Discord signs (X-Signature-Timestamp + raw_body)
with its application's private key. We verify against the public key stored in
SSM as a plain String (not a secret -- it is truly public, but we keep it in
SSM for consistent config management alongside the bot token).
"""

import base64
import json
import logging
import os

import boto3
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration -----------------------------------------------------------

DISPATCH_FUNCTION = os.environ.get("DISPATCH_FUNCTION", "dispatch-router")

# Public key: not a secret, but stored in SSM for consistent config.
# Bot token: secret, stored as SecureString. Only reply.py and the agent's
# discord_post tool need it. This Lambda never touches the bot token.
DISCORD_PUBLIC_KEY_PARAM = os.environ.get(
    "DISCORD_PUBLIC_KEY_PARAM", "/sdlc-agents/discord-public-key"
)

# Secrets and config live on an invocation-scoped dict (threat T-8 posture --
# mirrors asana_webhook.py). No module-level credential globals.
_ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")

# Module-level VerifyKey cache. The public key is not a secret (it is truly
# public — Discord publishes it in the Developer Portal). Caching the parsed
# VerifyKey avoids an SSM round-trip on every invocation and keeps cold-start
# signature verification well inside Discord's 3s PING deadline.
_verify_key: VerifyKey | None = None


def _get_ssm_param(name: str, with_decryption: bool = True) -> str:
    resp = _ssm.get_parameter(Name=name, WithDecryption=with_decryption)
    return resp["Parameter"]["Value"]


# --- Signature Verification --------------------------------------------------


def _verify_ed25519(public_key_hex: str, signature_hex: str, timestamp: str, raw_body: bytes) -> bool:
    """Verify the Ed25519 signature Discord attaches to every inbound request.

    Discord signs (timestamp_bytes + raw_body_bytes) with the application's
    private key. We verify against the published application public key.

    Uses pynacl (nacl.signing.VerifyKey). Returns True on a valid signature,
    False on any mismatch or malformed input. Never raises.

    No replay-window check is applied here; rationale is documented in
    docs/threat-model.md section T-9c.
    """
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        message = timestamp.encode() + raw_body
        sig_bytes = bytes.fromhex(signature_hex)
        verify_key.verify(message, sig_bytes)
        return True
    except BadSignatureError as exc:
        logger.warning("Ed25519 signature verification failed: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Ed25519 signature verification error: %s", exc)
        return False


# --- Agent Resolution --------------------------------------------------------

# Command names must match what bootstrap_discord_app.py registers via
# PUT /applications/{app_id}/commands. Each agent in .dispatch/agents.yaml
# that has discord: [slash_command] gets a command named after itself.
COMMAND_ALIAS_MAP = {
    "pm": "workitems",
    "status": "workitems",
    "plan": "workitems",
    "docs": "docwriter",
    "doc": "docwriter",
    "writer": "docwriter",
    "ba": "researcher",
    "research": "researcher",
    "analyze": "researcher",
    "decisions": "adr",
    "architecture": "adr",
}

KNOWN_AGENTS = {"workitems", "researcher", "docwriter", "adr"}


def _resolve_agent_id(command_name: str) -> str | None:
    """Map a slash command name to a canonical agent_id, or None if unknown."""
    name = command_name.lower()
    if name in KNOWN_AGENTS:
        return name
    return COMMAND_ALIAS_MAP.get(name)


# --- Dispatch ----------------------------------------------------------------


def dispatch(agent_id: str, instruction: str, sender: str, context: dict) -> None:
    """Async-invoke the Dispatch Router with a pre-resolved agent_id.

    Passing agent_id bypasses the Router's @mention extraction -- the command
    name already resolved the agent. Same pattern as asana_webhook.py dispatch().
    """
    payload = {
        "source": "discord",
        "trigger_type": "slash_command",
        "agent_id": agent_id,
        "body": instruction,
        "instruction": instruction,
        "sender": sender,
        "context": context,
    }
    logger.info("Dispatching to %s via discord slash_command", agent_id)
    lambda_client.invoke(
        FunctionName=DISPATCH_FUNCTION,
        InvocationType="Event",  # async -- we already acknowledged to Discord
        Payload=json.dumps(payload).encode(),
    )


# --- Interaction Processors --------------------------------------------------


def _process_application_command(interaction: dict) -> dict:
    """Handle a type-2 APPLICATION_COMMAND interaction.

    1. Acknowledge with type 5 (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE) so
       Discord shows "Bot is thinking..." for up to 15 minutes.
    2. Resolve the agent from the command name.
    3. Extract the 'instruction' option the user typed.
    4. Async-dispatch to the router -- agent posts follow-up via
       discord_post_followup(application_id, interaction_token, content).

    Returns the synchronous API Gateway response dict.
    """
    data = interaction.get("data", {})
    command_name = data.get("name", "")

    agent_id = _resolve_agent_id(command_name)
    if not agent_id:
        logger.warning("Unknown slash command: /%s", command_name)
        # Respond visibly so the user is not left with no feedback.
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                "data": {
                    "content": (
                        f"Unknown command `/{command_name}`. "
                        f"Valid commands: {sorted(KNOWN_AGENTS)}"
                    )
                },
            }),
        }

    # Extract the instruction from the options list.
    # bootstrap_discord_app.py registers a single required string option
    # named "instruction" for every agent command.
    options = data.get("options", [])
    instruction = ""
    for opt in options:
        if opt.get("name") == "instruction":
            instruction = str(opt.get("value", "")).strip()
            break
    if not instruction:
        instruction = (
            f"You were invoked via Discord /{command_name} with no instruction text. "
            "Introduce yourself and describe what you can do."
        )

    # Sender identity: stable Discord snowflake ID (threat T-4 analogue --
    # immutable, non-self-editable, the correct value for authorization
    # allowlist matching in .dispatch/agents.yaml).
    # member.user is populated in guild contexts; user is populated in DMs.
    member = interaction.get("member", {})
    user_obj = member.get("user") or interaction.get("user") or {}
    sender = user_obj.get("id", "")
    if not sender:
        logger.warning("Could not resolve Discord user ID from interaction payload")
        sender = "unknown"

    # Build the source_context that travels through the router into the agent.
    # application_id + interaction_token let the agent post a follow-up reply
    # via POST /webhooks/{application_id}/{interaction_token} (valid 15 min).
    context = {
        "guild_id": interaction.get("guild_id", ""),
        "channel_id": interaction.get("channel_id", ""),
        "application_id": interaction.get("application_id", ""),
        "interaction_token": interaction.get("token", ""),
        "user_id": sender,
    }

    # Dispatch asynchronously before returning -- Discord requires a response
    # within 3 seconds and the agent work can take far longer.
    try:
        dispatch(
            agent_id=agent_id,
            instruction=instruction,
            sender=sender,
            context=context,
        )
    except Exception as exc:
        logger.exception("Failed to dispatch discord command to router: %s", exc)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "type": 4,
                "data": {"content": "Dispatch failed -- please try again or contact an operator."},
            }),
        }

    # type 5 = DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE -- tells Discord we received
    # the command and will respond later. The agent sends the actual reply via
    # POST /webhooks/{application_id}/{interaction_token} using discord_post_followup.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"type": 5}),
    }


# --- Lambda Handler ----------------------------------------------------------


def handler(event, context):
    """Discord Interactions Endpoint Lambda handler.

    Fronted by API Gateway at POST /discord/interactions.

    Flow:
    1. Fetch Discord public key from SSM -- hard-fail 503 if missing (mirrors
       asana_webhook.py:307-321 posture; a missing param means the app is not
       registered and we must not accept unsigned events).
    2. Verify Ed25519 signature -- reject 401 on mismatch or missing headers.
    3. Parse interaction type.
    4. PING (type 1) -- respond {"type": 1}.
    5. APPLICATION_COMMAND (type 2) -- acknowledge with {"type": 5} (deferred),
       then async-invoke Dispatch Router.
    6. Unknown type -- 400.
    """
    global _verify_key

    headers = event.get("headers", {})
    # Normalize header keys to lowercase -- API Gateway may preserve original case.
    headers = {k.lower(): v for k, v in headers.items()}

    # API Gateway may base64-encode the body when binary content types are in
    # play. Decode to bytes so signature verification uses the exact wire bytes.
    raw_body_str = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body_bytes = base64.b64decode(raw_body_str)
    else:
        raw_body_bytes = raw_body_str.encode()

    # --- Fetch Discord public key from SSM (cached) --------------------------
    # Hard-fail on missing param. A misconfigured stage must not silently
    # accept unsigned events. Same posture as the Asana webhook secret check.
    # The VerifyKey is cached at module scope after first successful fetch to
    # keep cold-start signature verification inside Discord's 3s PING deadline.
    if _verify_key is None:
        try:
            public_key_hex = _get_ssm_param(DISCORD_PUBLIC_KEY_PARAM, with_decryption=False)
        except _ssm.exceptions.ParameterNotFoundException:
            logger.error(
                "Discord public-key parameter %s not found. Run "
                "scripts/bootstrap_discord_app.py to register the app.",
                DISCORD_PUBLIC_KEY_PARAM,
            )
            return {"statusCode": 503, "body": "discord app not registered"}
        except Exception as exc:
            logger.error("Failed to fetch Discord public key from SSM: %s", exc)
            return {"statusCode": 503, "body": "could not load discord config"}

        if not public_key_hex:
            logger.error("Discord public-key parameter %s is empty.", DISCORD_PUBLIC_KEY_PARAM)
            return {"statusCode": 503, "body": "discord app not registered"}

        _verify_key = VerifyKey(bytes.fromhex(public_key_hex))

    # --- Ed25519 Signature Verification ---------------------------------------
    # Both headers are required. Reject early on missing headers rather than
    # trying to verify with empty strings.
    sig_header = headers.get("x-signature-ed25519", "")
    ts_header = headers.get("x-signature-timestamp", "")

    if not sig_header or not ts_header:
        logger.warning(
            "Missing Discord signature headers (ed25519=%r, timestamp=%r)",
            sig_header,
            ts_header,
        )
        return {"statusCode": 401, "body": "missing signature headers"}

    if not _verify_ed25519(_verify_key.encode().hex(), sig_header, ts_header, raw_body_bytes):
        logger.warning("Discord Ed25519 signature verification failed")
        return {"statusCode": 401, "body": "invalid request signature"}

    # --- Parse interaction ---------------------------------------------------
    try:
        payload = json.loads(raw_body_bytes) if isinstance(raw_body_bytes, bytes) else raw_body_bytes
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    interaction_type = payload.get("type")
    logger.info("Discord interaction type=%s", interaction_type)

    # --- PING handshake (type 1) ---------------------------------------------
    # Discord POSTs type-1 whenever the operator saves the Interactions URL in
    # the Developer Portal. Must respond {"type": 1} within 3 seconds.
    if interaction_type == 1:
        logger.info("Discord PING handshake -- responding type 1")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 1}),
        }

    # --- APPLICATION_COMMAND (type 2) ----------------------------------------
    if interaction_type == 2:
        return _process_application_command(payload)

    # Unknown interaction type -- Discord may add new types in the future.
    logger.warning("Unhandled Discord interaction type: %s", interaction_type)
    return {"statusCode": 400, "body": f"unhandled interaction type {interaction_type}"}

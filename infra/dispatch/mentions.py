"""Shared webhook-receiver helpers for the dispatch trigger sources.

Every trigger source (GitHub, Asana, and future ones like Slack) fronts the
Dispatch Router with a public HTTPS webhook that must:

  1. authenticate the delivery by its HMAC-SHA256 signature, and
  2. resolve an ``@mention`` to a fleet agent against the LIVE registry — never a
     hardcoded roster, so an agent onboarded in the dashboard is reachable from
     EVERY source with no per-source code change.

Both concerns live here so there is ONE implementation to harden (timing-safe
compare, empty-secret handling) and ONE place to change when the registry shape
or signing scheme evolves. Each receiver stays a thin source-specific adapter:
it parses its own event shape and enriches context, then calls these helpers.

The registry is the same SSM parameter the Dispatch Router reads
(``config_store.render_registry`` → ``{"agents": {id: {aliases, ...}}}``); this
module only needs the agent ids and their aliases to resolve a mention.
"""

import hashlib
import hmac
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

# Any ``@word`` mention. The registry — not a hardcoded roster — decides which
# words are real agents/aliases; this pattern just enumerates the candidates.
# Mirrors router.MENTION_PATTERN; the router does the authoritative resolution.
MENTION_PATTERN = re.compile(r"@(\w+)", re.IGNORECASE)


def verify_hmac_sha256(secret: str, raw_body: str, provided_signature: str, *, prefix: str = "") -> bool:
    """Constant-time HMAC-SHA256 check over the EXACT raw request body.

    ``prefix`` matches how the source encodes the signature header: GitHub sends
    ``sha256=<hex>`` (prefix ``"sha256="``); Asana sends a bare ``<hex>`` (prefix
    ``""``). Callers MUST pass the unparsed body — signatures are computed over
    the bytes on the wire, not a re-serialized payload.
    """
    if not secret or not provided_signature:
        return False
    if prefix and not provided_signature.startswith(prefix):
        return False
    expected = prefix + hmac.new(
        secret.encode(), raw_body.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided_signature, expected)


def resolve_mention(body: str, registry: dict) -> tuple[str, str] | None:
    """Resolve the FIRST @mention in ``body`` that maps to a known agent.

    Returns ``(agent_id, instruction)`` where ``instruction`` is the text after
    that mention (trimmed), or ``None`` if no mention resolves. Checks each
    mention against the registry's agent ids AND their aliases, so a UI-onboarded
    agent (or a newly-added alias) resolves the moment it lands in the registry —
    no code change in any receiver.
    """
    agents = (registry or {}).get("agents", {})
    if not agents:
        return None
    for match in MENTION_PATTERN.finditer(body or ""):
        name = match.group(1).lower()
        if name in agents:
            agent_id = name
        else:
            agent_id = next(
                (aid for aid, cfg in agents.items() if name in (cfg.get("aliases") or [])),
                None,
            )
        if agent_id:
            return agent_id, body[match.end():].strip()
    return None


def resolve_agent(body: str, registry: dict) -> str | None:
    """The agent id of the first resolvable @mention in ``body``, or None. Use
    when the receiver keeps the full comment as the instruction (e.g. GitHub);
    use ``resolve_mention`` when it wants only the text after the mention."""
    resolved = resolve_mention(body, registry)
    return resolved[0] if resolved else None


class RegistryCache:
    """The agent registry, loaded from SSM and cached for a short TTL.

    The registry is runtime-mutable (an admin onboard/enable/disable republishes
    it immediately), so a lifetime cache on a warm Lambda would keep resolving to
    a just-disabled agent — or miss a just-onboarded one — until a cold start.
    The TTL bounds that staleness fleet-wide, matching the Dispatch Router's own
    registry cache and ``fleet_config``'s short-TTL config cache.

    ``ssm_provider`` is a zero-arg callable returning the boto3 SSM client,
    resolved on each load so a receiver's test can swap its client after import.
    """

    def __init__(self, param_name: str, ssm_provider, *, ttl_seconds: int = 30):
        self._param = param_name
        self._ssm_provider = ssm_provider
        self._ttl = ttl_seconds
        self._cache: dict | None = None
        self._expires_at = 0.0

    def load(self) -> dict:
        """Return the cached registry, refreshing when the TTL has expired. On a
        read error, serves the last-known-good cache (or an empty registry) so a
        transient SSM hiccup degrades to 'mention not resolved', never an
        exception that 500s the whole delivery."""
        now = time.time()
        if self._cache is not None and now < self._expires_at:
            return self._cache
        try:
            resp = self._ssm_provider().get_parameter(Name=self._param, WithDecryption=False)
            # publish_registry writes compact JSON (a YAML subset); parse as JSON
            # to avoid a PyYAML dependency in the receivers.
            self._cache = json.loads(resp["Parameter"]["Value"]) or {}
            self._expires_at = now + self._ttl
        except Exception:  # noqa: BLE001
            logger.exception("could not load agent registry from %s", self._param)
            return self._cache if self._cache is not None else {}
        return self._cache

    def resolve_agent(self, body: str) -> str | None:
        return resolve_agent(body, self.load())

    def resolve_mention(self, body: str) -> tuple[str, str] | None:
        return resolve_mention(body, self.load())

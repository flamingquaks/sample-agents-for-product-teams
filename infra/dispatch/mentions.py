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


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    raw_body: str,
    provided_signature: str,
    *,
    max_skew_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """Constant-time verification of a Slack request signature.

    Slack does NOT sign the bare body: it signs the basestring
    ``v0:{timestamp}:{raw_body}`` with HMAC-SHA256 and sends
    ``X-Slack-Signature: v0=<hex>`` plus ``X-Slack-Request-Timestamp: <epoch>``.
    We (1) reject a missing secret/signature/timestamp, (2) reject a timestamp
    outside ±``max_skew_seconds`` (replay protection — an old capture can't be
    re-sent), then (3) timing-safe compare. ``raw_body`` MUST be the exact bytes
    on the wire (decode API-Gateway base64 BEFORE calling this)."""
    if not signing_secret or not provided_signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        return False
    basestring = f"v0:{timestamp}:{raw_body}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided_signature, expected)


class ForgeTokenError(Exception):
    """A Forge Invocation Token failed verification (bad signature / expiry /
    audience / app id, or the JWKS was unreachable). The receiver maps a
    verification failure to 401 and a JWKS-unreachable to 503 (fail closed)."""

    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


# Atlassian's published JWKS — RS256 keys used to sign every Forge Invocation
# Token. Fetched + cached with a TTL; kid-rotation is tolerated (a kid miss
# forces one refetch). Overridable via FORGE_JWKS_URL for tests/staging.
_FORGE_JWKS_DEFAULT_URL = "https://forge.cdn.prod.atlassian-dev.net/.well-known/jwks.json"
_jwks_cache: dict | None = None
_jwks_expires_at = 0.0
_JWKS_TTL_SECONDS = 3600


def _jwks_url() -> str:
    import os

    return os.environ.get("FORGE_JWKS_URL") or _FORGE_JWKS_DEFAULT_URL


def _load_jwks(*, force: bool = False) -> dict:
    """Fetch + cache Atlassian's JWKS. Raises ForgeTokenError(unavailable=True)
    when the endpoint can't be reached (the receiver returns 503 → Forge retries,
    never accepting an unverifiable delivery)."""
    global _jwks_cache, _jwks_expires_at
    now = time.time()
    if not force and _jwks_cache is not None and now < _jwks_expires_at:
        return _jwks_cache
    import requests

    try:
        resp = requests.get(_jwks_url(), timeout=5)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_expires_at = now + _JWKS_TTL_SECONDS
    except Exception as exc:  # noqa: BLE001
        raise ForgeTokenError(f"could not fetch Forge JWKS: {exc}", unavailable=True) from exc
    return _jwks_cache


def _jwk_for_kid(kid: str, *, allow_refetch: bool = True) -> dict | None:
    jwks = _load_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    if allow_refetch:
        # kid rotation — force one refetch before giving up.
        jwks = _load_jwks(force=True)
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
    return None


def verify_forge_invocation_token(
    token: str,
    *,
    expected_app_id: str,
    expected_audience: str | None = None,
    now: float | None = None,
) -> dict:
    """Verify a Forge Invocation Token (FIT) and return its claims (§A5).

    A FIT is an asymmetrically-signed (RS256) JWT. We verify the signature
    against Atlassian's published JWKS, the expiry, the audience (when supplied),
    and the pinned Forge ``app`` id. There is NO shared secret — nothing to
    capture, store, or rotate; the app id is pinned at first verified delivery.

    Raises ForgeTokenError on any failure: a signature/claim mismatch is a hard
    reject (→ 401); a JWKS-unreachable is ``unavailable=True`` (→ 503, fail
    closed, Forge retries). Returns the decoded claims on success — the caller
    reads the installation ``cloudId`` from them and cross-checks the site row.
    """
    if not token:
        raise ForgeTokenError("missing Forge invocation token")
    try:
        import jwt as _jwt  # PyJWT
        from jwt import PyJWKClient  # noqa: F401  (import guard)
    except Exception as exc:  # noqa: BLE001
        raise ForgeTokenError(f"JWT library unavailable: {exc}", unavailable=True) from exc
    try:
        header = _jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        raise ForgeTokenError(f"malformed token header: {exc}") from exc
    kid = header.get("kid")
    if not kid:
        raise ForgeTokenError("token header missing kid")
    jwk = _jwk_for_kid(kid)
    if jwk is None:
        raise ForgeTokenError(f"no JWKS key for kid {kid!r}")
    try:
        from jwt.algorithms import RSAAlgorithm

        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        options = {"require": ["exp"], "verify_aud": expected_audience is not None}
        claims = _jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=expected_audience if expected_audience else None,
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 — expired/bad-sig/bad-aud all reject
        raise ForgeTokenError(f"token verification failed: {exc}") from exc
    # Pin the Forge app id — the token's ``app`` (or ``app.id``) claim must match
    # the site row's recorded forge_app_id, so a token from a DIFFERENT Forge app
    # (even correctly signed by Atlassian) is rejected. When the site has no
    # pinned id yet (expected_app_id == ""), the caller pins it from these claims
    # on this first verified delivery.
    app_id = forge_app_id_from_claims(claims)
    if expected_app_id and app_id != expected_app_id:
        raise ForgeTokenError(
            f"token app id {app_id!r} does not match pinned {expected_app_id!r}"
        )
    return claims


def forge_app_id_from_claims(claims: dict) -> str:
    """The Forge app id (ari) a FIT's claims carry — the ``app`` claim, which is
    either a bare string or an object with an ``id``. "" when absent. Shared by
    the verify path (pin match) and the receivers (first-delivery pin)."""
    app_claim = (claims or {}).get("app")
    app_id = app_claim.get("id") if isinstance(app_claim, dict) else app_claim
    return str(app_id or "")


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

"""GitHub App installation-token minting for agent runtimes (direct mode).

Replaces the single shared PAT with a per-OWNER GitHub App installation token,
minted just-in-time for the owner of the repo the agent was dispatched against.
An installation token is bounded by construction — it can only touch repos in
that owner's installation, with the App's permission set — so it closes the
"one over-broad PAT" gap (threat-model T-11) at the credential layer.

Selected by ``GITHUB_AUTH_MODE`` (default ``pat`` — no behavior change until an
operator flips it to ``app`` per stage): the agent's github client asks for a
token for the dispatched owner instead of reading the PAT.

How the installation is resolved (no GitHub round-trip in the hot path):
  onboarding already recorded a per-owner install record in the fleet-config
  DynamoDB table (``pk="owner#<owner>"``, written by the admin API's
  ``config_store.put_installation``). We read ``installation_id`` from there. If
  that record is somehow absent, we fall back to GitHub's per-owner installation
  lookup with the App JWT.

Credential storage (matches infra/dashboard/github_client.py, the onboarding
side): the App private key (PEM) is in Secrets Manager
(``GITHUB_APP_PRIVATE_KEY_SECRET_ARN``); the app id is in SSM String
(``GITHUB_APP_ID_PARAM``). This module is the agent-runtime mirror of the
onboarding minter — same JWT/token mechanics, different installation source
(DynamoDB record vs. a live lookup).
"""

import calendar
import json
import os
import time
import urllib.error
import urllib.request

import boto3

GITHUB_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"

_OWNER_PK_PREFIX = "owner#"

_secrets = None
_ssm = None
_ddb_table = None

_pem_cache: tuple[str, float] | None = None  # (pem, expires_at)
_PEM_TTL = 1800
# App-JWT cache — one sign serves a whole invocation. Valid ~9 min.
_jwt_cache: tuple[str, float] | None = None
_JWT_REFRESH_MARGIN = 30
# installation-token cache keyed by installation_id → (token, expires_at_epoch)
_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_REFRESH_MARGIN = 60


class GitHubAppError(Exception):
    """Minting a GitHub App installation token failed."""


def app_mode() -> bool:
    """Whether the fleet is configured to use per-owner GitHub App installation
    tokens (GITHUB_AUTH_MODE=app) rather than the legacy shared PAT."""
    return os.environ.get("GITHUB_AUTH_MODE", "pat").lower() == "app"


def _sm():
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets


def _ssm_client():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _table():
    global _ddb_table
    if _ddb_table is None:
        name = os.environ["FLEET_CONFIG_TABLE"]
        _ddb_table = boto3.resource("dynamodb").Table(name)
    return _ddb_table


def _app_id() -> str:
    return _ssm_client().get_parameter(Name=os.environ["GITHUB_APP_ID_PARAM"])[
        "Parameter"
    ]["Value"]


def _private_key() -> str:
    global _pem_cache
    now = time.time()
    if _pem_cache and now < _pem_cache[1]:
        return _pem_cache[0]
    arn = os.environ["GITHUB_APP_PRIVATE_KEY_SECRET_ARN"]
    raw = _sm().get_secret_value(SecretId=arn).get("SecretString") or ""
    pem = raw
    if raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw)
            pem = parsed.get("privateKey") or parsed.get("pem") or raw
        except json.JSONDecodeError:
            pem = raw
    _pem_cache = (pem, now + _PEM_TTL)
    return pem


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _app_jwt() -> str:
    """A cached ~9-minute App JWT (iss = App ID), signed RS256 with the App key."""
    global _jwt_cache
    now = time.time()
    if _jwt_cache and now < _jwt_cache[1] - _JWT_REFRESH_MARGIN:
        return _jwt_cache[0]

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    issued = int(now)
    exp = issued + 540
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": issued - 60, "exp": exp, "iss": _app_id()}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}".encode()
    key = serialization.load_pem_private_key(_private_key().encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{signing_input.decode()}.{_b64url(signature)}"
    _jwt_cache = (jwt, float(exp))
    return jwt


def _request(method: str, url: str, *, token: str):
    headers = {
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — fixed https host
            payload = resp.read().decode()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        # Do NOT echo the response body — it can carry token/JWT material.
        raise GitHubAppError(f"GitHub {method} {url} -> {exc.code}") from None
    except urllib.error.URLError as exc:
        raise GitHubAppError(f"GitHub {method} {url} unreachable: {exc.reason}") from exc


def _installation_id_for(owner: str) -> int:
    """The App installation id for ``owner``. Prefer the per-owner record written
    at onboard (no GitHub round-trip); fall back to a live lookup if it's absent."""
    normalized = owner.strip().casefold()
    try:
        item = (
            _table()
            .get_item(Key={"pk": f"{_OWNER_PK_PREFIX}{normalized}"})
            .get("Item")
        )
    except Exception:  # noqa: BLE001 — table read failed; try the live lookup
        item = None
    if item and item.get("installation_id") is not None:
        return int(item["installation_id"])

    # Fallback: ask GitHub. Try org then user shape (we don't know owner_type here).
    for path in ("orgs", "users"):
        try:
            _status, data = _request(
                "GET",
                f"{GITHUB_API_BASE}/{path}/{owner}/installation",
                token=_app_jwt(),
            )
            if data.get("id") is not None:
                return int(data["id"])
        except GitHubAppError:
            continue
    raise GitHubAppError(f"no GitHub App installation found for owner '{owner}'")


def installation_token_for_owner(owner: str) -> str:
    """A short-lived installation token scoped to ``owner``'s installation,
    cached until shortly before expiry. Raises GitHubAppError on failure."""
    if not owner:
        raise GitHubAppError("owner is required to mint an installation token")
    installation_id = _installation_id_for(owner)

    now = time.time()
    cached = _token_cache.get(installation_id)
    if cached and now < cached[1] - _TOKEN_REFRESH_MARGIN:
        return cached[0]

    _status, data = _request(
        "POST",
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        token=_app_jwt(),
    )
    token = data.get("token")
    if not token:
        raise GitHubAppError(
            f"GitHub returned no token for installation {installation_id}"
        )
    # GitHub returns UTC ("...Z"); parse with calendar.timegm (NOT time.mktime,
    # which assumes local time and skews the cached expiry off UTC hosts).
    try:
        exp_epoch = calendar.timegm(
            time.strptime(data.get("expires_at", ""), "%Y-%m-%dT%H:%M:%SZ")
        )
    except (ValueError, TypeError):
        exp_epoch = now + 3300
    _token_cache[installation_id] = (token, exp_epoch)
    return token


def token_for_dispatch(owner_or_repo: str) -> str:
    """Convenience: accept either ``owner`` or ``owner/repo`` (the dispatch
    carries ``owner/repo``) and return an installation token for its owner."""
    owner = (owner_or_repo or "").split("/", 1)[0]
    return installation_token_for_owner(owner)

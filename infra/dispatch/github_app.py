"""GitHub App installation-token minting for the dispatch reply Lambda.

The reply Lambda posts a short "blocked by safety filter" note back to the
originating GitHub issue/PR. When ``GITHUB_AUTH_MODE=app`` it authenticates as
the fleet's GitHub App with a per-owner installation token (bounded to that
owner's installed repos — threat-model T-11) instead of the shared PAT.

This is the dispatch-side mirror of infra/dashboard/github_client.py and
agents/shared/tools/github_app.py — same JWT/token mechanics, but built on
``requests`` (already a dispatch dependency) and resolving the installation id
from the fleet-config DynamoDB table's per-owner record (written at onboard).
"""

import calendar
import json
import logging
import os
import time

import boto3
import requests

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"
_OWNER_PK_PREFIX = "owner#"

_secrets = None
_ssm = None
_ddb_table = None

_pem_cache: tuple[str, float] | None = None
_PEM_TTL = 1800
_jwt_cache: tuple[str, float] | None = None
_JWT_REFRESH_MARGIN = 30
_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_REFRESH_MARGIN = 60


class GitHubAppError(Exception):
    """Minting a GitHub App installation token failed."""


def app_mode() -> bool:
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
        _ddb_table = boto3.resource("dynamodb").Table(os.environ["FLEET_CONFIG_TABLE"])
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
    raw = (
        _sm()
        .get_secret_value(SecretId=os.environ["GITHUB_APP_PRIVATE_KEY_SECRET_ARN"])
        .get("SecretString")
        or ""
    )
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


def _installation_id_for(owner: str) -> int:
    normalized = owner.strip().casefold()
    try:
        item = (
            _table().get_item(Key={"pk": f"{_OWNER_PK_PREFIX}{normalized}"}).get("Item")
        )
    except Exception:  # noqa: BLE001
        item = None
    if item and item.get("installation_id") is not None:
        return int(item["installation_id"])

    jwt = _app_jwt()
    for path in ("orgs", "users"):
        resp = requests.get(
            f"{GITHUB_API_BASE}/{path}/{owner}/installation",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            },
            timeout=10,
        )
        if resp.status_code == 200 and resp.json().get("id") is not None:
            return int(resp.json()["id"])
    raise GitHubAppError(f"no GitHub App installation found for owner '{owner}'")


def installation_token_for_repo(repo: str) -> str:
    """A short-lived installation token scoped to ``repo``'s owner. ``repo`` is
    ``owner/repo``. Raises GitHubAppError on failure."""
    owner = (repo or "").split("/", 1)[0]
    if not owner:
        raise GitHubAppError("repo/owner required to mint an installation token")
    installation_id = _installation_id_for(owner)

    now = time.time()
    cached = _token_cache.get(installation_id)
    if cached and now < cached[1] - _TOKEN_REFRESH_MARGIN:
        return cached[0]

    resp = requests.post(
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
        },
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        # Do not log the body — it can carry token material.
        raise GitHubAppError(
            f"installation token request failed ({resp.status_code}) for {repo}"
        )
    data = resp.json()
    token = data.get("token")
    if not token:
        raise GitHubAppError(f"GitHub returned no token for installation {installation_id}")
    try:
        exp_epoch = calendar.timegm(
            time.strptime(data.get("expires_at", ""), "%Y-%m-%dT%H:%M:%SZ")
        )
    except (ValueError, TypeError):
        exp_epoch = now + 3300
    _token_cache[installation_id] = (token, exp_epoch)
    return token

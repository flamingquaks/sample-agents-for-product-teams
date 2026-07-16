"""GitHub App auth engine — manifest flow, App JWT, installation tokens, lookups.

Used by the admin API to (1) create the fleet's GitHub App from a manifest, and
(2) verify at onboard time that the App is installed on a repo's owner and can
reach the repo, minting a short-lived installation token to do so. Retires the
single shared PAT in favor of per-owner installation tokens (see
docs/specs/github-onboarding-spec.md; threat-model T-11).

Secret storage (per the spec's split):
  - private key (PEM)  → Secrets Manager  (GITHUB_APP_PRIVATE_KEY_SECRET_ARN)
  - app id, app slug   → SSM String       (GITHUB_APP_ID_PARAM, GITHUB_APP_SLUG_PARAM)

GitHub API facts verified against docs.github.com (2026):
  - manifest form POSTs field ``manifest`` to /settings/apps/new (user) or
    /organizations/<org>/settings/apps/new (org); code valid ~1h.
  - POST /app-manifests/{code}/conversions needs NO auth (the code authorizes it)
    and returns id, slug, pem, client_secret, webhook_secret, ...
  - App JWT: RS256, iss = App ID, exp ≤ 10 min, Authorization: Bearer.
  - installation lookups + POST /app/installations/{id}/access_tokens need the
    App JWT; installation token lives ~1h.
"""

import json
import os
import time
import urllib.error
import urllib.request

import boto3

GITHUB_API = "https://github.com"
GITHUB_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"

# App permissions the fleet requests — the union ceiling for all agents (Cedar
# narrows per agent below this, and the destructive forbid still blocks
# merge/delete/close regardless). See spec §3.1.
APP_PERMISSIONS = {
    "metadata": "read",
    "issues": "write",
    "pull_requests": "write",
    "contents": "write",
}
APP_EVENTS = ["issue_comment", "pull_request", "pull_request_review", "push"]


class GitHubError(Exception):
    """A GitHub API call failed. ``status`` is the HTTP status when known."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- clients / config --------------------------------------------------------

_secrets = None
_ssm = None
_pem_cache: tuple[str, float] | None = None  # (pem, expires_at)
_PEM_TTL = 1800  # re-read the key from Secrets Manager at most every 30 min
# installation-token cache keyed by installation_id → (token, expires_at_epoch)
_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_REFRESH_MARGIN = 60  # refresh a bit before the 1h expiry


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


def app_configured() -> bool:
    """Whether the fleet GitHub App has been registered (its id + key present)."""
    return bool(
        os.environ.get("GITHUB_APP_ID_PARAM")
        and os.environ.get("GITHUB_APP_PRIVATE_KEY_SECRET_ARN")
    )


def _app_id() -> str:
    return _ssm_client().get_parameter(Name=os.environ["GITHUB_APP_ID_PARAM"])[
        "Parameter"
    ]["Value"]


def app_slug() -> str | None:
    param = os.environ.get("GITHUB_APP_SLUG_PARAM")
    if not param:
        return None
    try:
        return _ssm_client().get_parameter(Name=param)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 — slug is best-effort (drives the install link)
        return None


def _private_key() -> str:
    global _pem_cache
    now = time.time()
    if _pem_cache and now < _pem_cache[1]:
        return _pem_cache[0]
    arn = os.environ["GITHUB_APP_PRIVATE_KEY_SECRET_ARN"]
    resp = _sm().get_secret_value(SecretId=arn)
    raw = resp.get("SecretString") or ""
    # Stored either as raw PEM or JSON {"privateKey": "..."} — accept both.
    pem = raw
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            pem = json.loads(raw).get("privateKey") or json.loads(raw).get("pem") or raw
        except json.JSONDecodeError:
            pem = raw
    _pem_cache = (pem, now + _PEM_TTL)
    return pem


# --- HTTP helper -------------------------------------------------------------


def _request(
    method: str, url: str, *, token: str | None = None, body: dict | None = None
):
    headers = {"Accept": _ACCEPT, "X-GitHub-Api-Version": _API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — fixed https host
            payload = resp.read().decode()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()
        except Exception:  # noqa: BLE001
            pass
        raise GitHubError(
            f"GitHub {method} {url} -> {exc.code}: {detail[:300]}", status=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub {method} {url} unreachable: {exc.reason}") from exc


# --- JWT (RS256, hand-signed via cryptography) -------------------------------


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _app_jwt() -> str:
    """A ~9-minute App JWT (iss = App ID) signed RS256 with the App private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": _app_id()}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}".encode()
    key = serialization.load_pem_private_key(_private_key().encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


# --- manifest flow -----------------------------------------------------------


def generate_manifest(app_name: str, api_base_url: str, frontend_url: str) -> dict:
    """The GitHub App manifest the admin UI POSTs to github.com. Least-privilege
    permissions; webhook disabled (the fleet dispatches via @mention, not the
    App webhook — spec §8)."""
    return {
        "name": app_name,
        "url": frontend_url,
        "hook_attributes": {"url": f"{api_base_url}/webhooks/github", "active": False},
        "redirect_url": f"{frontend_url}#/admin/github-app/setup-callback",
        "public": False,
        "default_permissions": APP_PERMISSIONS,
        "default_events": APP_EVENTS,
    }


def exchange_manifest_code(code: str) -> dict:
    """Exchange the temporary manifest code for the created App's credentials and
    persist them (PEM → Secrets Manager, id + slug → SSM). Returns non-secret
    fields (id, slug) for the caller. No auth needed on this call."""
    status, data = _request(
        "POST", f"{GITHUB_API_BASE}/app-manifests/{code}/conversions"
    )
    app_id = str(data.get("id", ""))
    slug = data.get("slug", "")
    pem = data.get("pem", "")
    if not app_id or not pem:
        raise GitHubError("manifest conversion returned no app id / private key")

    _sm().put_secret_value(
        SecretId=os.environ["GITHUB_APP_PRIVATE_KEY_SECRET_ARN"], SecretString=pem
    )
    ssm = _ssm_client()
    ssm.put_parameter(
        Name=os.environ["GITHUB_APP_ID_PARAM"],
        Value=app_id,
        Type="String",
        Overwrite=True,
    )
    if os.environ.get("GITHUB_APP_SLUG_PARAM") and slug:
        ssm.put_parameter(
            Name=os.environ["GITHUB_APP_SLUG_PARAM"],
            Value=slug,
            Type="String",
            Overwrite=True,
        )
    # Reset caches so subsequent calls use the newly-stored key/id.
    global _pem_cache
    _pem_cache = None
    _token_cache.clear()
    return {"app_id": app_id, "slug": slug}


def install_url(owner: str | None = None) -> str | None:
    """Deep-link to install the App. Owner is informational; GitHub's install UI
    lets the admin pick the account/repos. Uses the app SLUG (not display name)."""
    slug = app_slug()
    return f"{GITHUB_API}/apps/{slug}/installations/new" if slug else None


# --- installation lookup + token ---------------------------------------------


def get_owner_type(owner: str) -> str:
    """'User' | 'Organization' for a GitHub account. Raises GitHubError (404) if
    the owner doesn't exist."""
    _status, data = _request(
        "GET", f"{GITHUB_API_BASE}/users/{owner}", token=_app_jwt()
    )
    return data.get("type", "User")


def find_installation(owner: str, owner_type: str) -> int | None:
    """The App's installation id on ``owner``, or None if the App isn't installed
    there. Uses the org- or user-scoped lookup per owner type."""
    path = "orgs" if owner_type == "Organization" else "users"
    try:
        _status, data = _request(
            "GET",
            f"{GITHUB_API_BASE}/{path}/{owner}/installation",
            token=_app_jwt(),
        )
    except GitHubError as exc:
        if exc.status == 404:
            return None  # App not installed on this owner
        raise
    return data.get("id")


def mint_installation_token(installation_id: int) -> tuple[str, str]:
    """A short-lived installation access token (cached ~until expiry). Returns
    (token, expires_at_iso)."""
    now = time.time()
    cached = _token_cache.get(installation_id)
    if cached and now < cached[1] - _TOKEN_REFRESH_MARGIN:
        # Return cached token; expires_at is re-derivable but callers rarely need
        # it when cached, so hand back a placeholder ISO from the cached epoch.
        return cached[0], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cached[1]))
    _status, data = _request(
        "POST",
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        token=_app_jwt(),
    )
    token = data["token"]
    expires_at = data.get("expires_at", "")
    # Parse expiry to epoch for the cache; fall back to now+55min if unparseable.
    try:
        exp_epoch = time.mktime(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        exp_epoch = now + 3300
    _token_cache[installation_id] = (token, exp_epoch)
    return token, expires_at


def repo_reachable(owner: str, repo: str, installation_id: int) -> bool:
    """Whether the installation can actually reach ``owner/repo`` (200 vs 404).
    Confirms the repo is included in the App's installation selection."""
    token, _ = mint_installation_token(installation_id)
    try:
        _request("GET", f"{GITHUB_API_BASE}/repos/{owner}/{repo}", token=token)
        return True
    except GitHubError as exc:
        if exc.status == 404:
            return False
        raise

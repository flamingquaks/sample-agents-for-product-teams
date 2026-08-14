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

import calendar
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
    # Advisory commit statuses — the reviewer agent publishes its verdict on the
    # PR head SHA (never a blocking failure state; see scm_broker).
    "statuses": "write",
}
# ``issue_comment`` / ``pull_request_review_comment`` drive @mention dispatch;
# ``issues`` + ``pull_request`` additionally feed SCM notifications (spec §18.3 —
# PR opened/merged/review-requested, issue opened → subscribed Slack channels).
APP_EVENTS = [
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "issues",
    "push",
]


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
# App-JWT cache — one sign serves every call in a request (a single onboard hits
# find_repo_installation + mint_installation_token). Valid ~9 min.
_jwt_cache: tuple[str, float] | None = None  # (jwt, expires_at_epoch)
_JWT_REFRESH_MARGIN = 30


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


# The template seeds the app-id/slug SSM params with this placeholder (SSM
# String can't be empty); the manifest flow overwrites it with the real id.
_UNSET = "unset"


def app_configured() -> bool:
    """Whether the fleet GitHub App has actually been registered AND usable — the
    app-id SSM param holds a real value (not the deploy-time placeholder) AND the
    private-key secret holds a PEM. Checking the env-var names alone is
    insufficient (they're always set once the dashboard is deployed); checking
    only the app-id is also insufficient — an app-id with an empty key secret
    would report 'configured' but then 500 on the first JWT sign
    (load_pem_private_key('')). Both must be real for the App to actually work."""
    if not (
        os.environ.get("GITHUB_APP_ID_PARAM")
        and os.environ.get("GITHUB_APP_PRIVATE_KEY_SECRET_ARN")
    ):
        return False
    try:
        if _app_id().strip() in ("", _UNSET):
            return False
        # Confirm the key secret has a usable PEM (not the empty placeholder the
        # template seeds), so 'configured' implies the first sign will succeed.
        return "PRIVATE KEY" in _private_key()
    except Exception:  # noqa: BLE001 — param/secret missing/unreadable → not configured
        return False


def _app_id() -> str:
    return _ssm_client().get_parameter(Name=os.environ["GITHUB_APP_ID_PARAM"])[
        "Parameter"
    ]["Value"]


def app_slug() -> str | None:
    param = os.environ.get("GITHUB_APP_SLUG_PARAM")
    if not param:
        return None
    try:
        value = _ssm_client().get_parameter(Name=param)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 — slug is best-effort (drives the install link)
        return None
    return None if value.strip() in ("", _UNSET) else value


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
    """A ~9-minute App JWT (iss = App ID) signed RS256 with the App private key.

    Cached until shortly before expiry so a single onboarding (get_owner_type +
    find_installation + mint_installation_token) does ONE RS256 sign + ONE app-id
    read + ONE PEM load instead of three."""
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


# --- manifest flow -----------------------------------------------------------


def generate_manifest(
    app_name: str, api_base_url: str, frontend_url: str, webhook_url: str
) -> dict:
    """The GitHub App manifest the admin UI POSTs to github.com. Least-privilege
    permissions; the webhook is ENABLED and points at the fleet's dispatch-side
    webhook receiver (``webhook_url``), which turns @mention comment events into
    dispatches — the server-side replacement for the retired per-repo
    agent-dispatch.yml workflow. GitHub returns a generated webhook secret from
    the manifest conversion, which exchange_manifest_code persists for the
    receiver to verify delivery signatures."""
    # redirect_url must be a fragment-free URL — GitHub rejects a "#..." hash
    # ("redirect_url must be a valid URL"). The SPA is hash-routed, but CloudFront
    # serves any path as index.html (403/404 → /), so we use a real PATH
    # (/github-app-callback) and the SPA detects it on load, exchanges ?code, then
    # navigates to the #/admin hash route.
    base = frontend_url.rstrip("/")
    return {
        "name": app_name,
        "url": frontend_url,
        "hook_attributes": {"url": webhook_url, "active": True},
        "redirect_url": f"{base}/github-app-callback",
        "public": False,
        "default_permissions": APP_PERMISSIONS,
        "default_events": APP_EVENTS,
    }


def exchange_manifest_code(code: str) -> dict:
    """Exchange the temporary manifest code for the created App's credentials and
    persist them (PEM → Secrets Manager, id + slug → SSM). Returns non-secret
    fields (id, slug) for the caller. No auth needed on this call."""
    try:
        _status, data = _request(
            "POST", f"{GITHUB_API_BASE}/app-manifests/{code}/conversions"
        )
    except GitHubError as exc:
        # SECURITY: the request URL embeds the manifest ``code``, which is still
        # redeemable (~1h, only consumed on success) and converts into the App's
        # private key + client_secret + webhook_secret. _request puts the full URL
        # in the error message; re-raise WITHOUT it so the code never reaches the
        # HTTP response body or CloudWatch logs (admin.py surfaces exc verbatim).
        raise GitHubError(
            f"manifest code exchange failed ({exc.status or 'network'})",
            status=exc.status,
        ) from None
    app_id = str(data.get("id", ""))
    slug = data.get("slug", "")
    pem = data.get("pem", "")
    if not app_id or not pem:
        raise GitHubError("manifest conversion returned no app id / private key")

    # Write order matters for crash-consistency: app_configured() keys ONLY on
    # the app-id param, so write everything else FIRST and the app-id LAST as the
    # commit marker. If any earlier write fails, app-id is never written,
    # app_configured() stays False, and a retry is clean (no half-registered App
    # that reports configured but has no key/slug).
    _sm().put_secret_value(
        SecretId=os.environ["GITHUB_APP_PRIVATE_KEY_SECRET_ARN"], SecretString=pem
    )
    ssm = _ssm_client()
    # The App's webhook secret — used by the github_webhook receiver Lambda to
    # verify each delivery's HMAC signature. Written BEFORE the app-id commit
    # marker (same crash-consistency rule as the key/slug): if this write fails,
    # app-id is never written and the whole registration retries cleanly. Stored
    # SecureString; the receiver reads it per-invocation. Only present when the
    # manifest enabled the webhook (it does — see generate_manifest).
    webhook_secret = data.get("webhook_secret", "")
    if os.environ.get("GITHUB_WEBHOOK_SECRET_PARAM") and webhook_secret:
        ssm.put_parameter(
            Name=os.environ["GITHUB_WEBHOOK_SECRET_PARAM"],
            Value=webhook_secret,
            Type="SecureString",
            Overwrite=True,
        )
    if os.environ.get("GITHUB_APP_SLUG_PARAM") and slug:
        ssm.put_parameter(
            Name=os.environ["GITHUB_APP_SLUG_PARAM"],
            Value=slug,
            Type="String",
            Overwrite=True,
        )
    ssm.put_parameter(
        Name=os.environ["GITHUB_APP_ID_PARAM"],
        Value=app_id,
        Type="String",
        Overwrite=True,
    )
    # Reset caches so subsequent calls use the newly-stored key/id (a new app-id
    # invalidates any cached JWT signed under the old one).
    global _pem_cache, _jwt_cache
    _pem_cache = None
    _jwt_cache = None
    _token_cache.clear()
    return {"app_id": app_id, "slug": slug}


def install_url() -> str | None:
    """Deep-link to install the App. GitHub's install UI lets the admin pick the
    account/repos, so no owner is passed. Uses the app SLUG (not display name)."""
    slug = app_slug()
    return f"{GITHUB_API}/apps/{slug}/installations/new" if slug else None


# --- installation lookup + token ---------------------------------------------


def find_repo_installation(owner: str, repo: str) -> tuple[int, str] | None:
    """The App installation that covers ``owner/repo`` → (installation_id,
    owner_type), or None if the App can't reach the repo (not installed on the
    owner, OR installed but the repo isn't in its repository selection).

    One JWT call to GET /repos/{owner}/{repo}/installation — the App-JWT-scoped
    endpoint made for exactly this question. (GET /users/{owner} does NOT accept
    an App JWT — GitHub returns 401 Bad credentials — which is why verification
    must never route through a user lookup.)"""
    try:
        _status, data = _request(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/installation",
            token=_app_jwt(),
        )
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    install_id = data.get("id")
    if install_id is None:
        return None
    return int(install_id), (data.get("account") or {}).get("type", "User")


def find_installation(owner: str) -> tuple[int, str] | None:
    """The App's installation on ``owner`` → (installation_id, owner_type), or
    None if the App isn't installed there. Tries the org lookup then the user
    lookup — both accept the App JWT, and probing both avoids a separate
    owner-type call (which has no JWT-accepting endpoint)."""
    for path, owner_type in (("orgs", "Organization"), ("users", "User")):
        try:
            _status, data = _request(
                "GET",
                f"{GITHUB_API_BASE}/{path}/{owner}/installation",
                token=_app_jwt(),
            )
        except GitHubError as exc:
            if exc.status == 404:
                continue  # not installed under this owner type
            raise
        if data.get("id") is not None:
            # The account's real type (an org 404s the user path anyway, but
            # trust the response over the path we happened to hit).
            return int(data["id"]), (data.get("account") or {}).get("type", owner_type)
    return None


def list_installations() -> list[dict]:
    """Every installation of the App → [{installation_id, owner, owner_type}].
    Paginated GET /app/installations with the App JWT."""
    installs: list[dict] = []
    page = 1
    while True:
        _status, data = _request(
            "GET",
            f"{GITHUB_API_BASE}/app/installations?per_page=100&page={page}",
            token=_app_jwt(),
        )
        if not isinstance(data, list) or not data:
            break
        for inst in data:
            account = inst.get("account") or {}
            installs.append(
                {
                    "installation_id": inst.get("id"),
                    "owner": account.get("login", ""),
                    "owner_type": account.get("type", "User"),
                }
            )
        if len(data) < 100:
            break
        page += 1
    return installs


def list_installation_repos(installation_id: int) -> list[dict]:
    """The repos an installation can reach → [{repo: "owner/name", private}].
    Paginated GET /installation/repositories with an installation token — this is
    the ground truth for 'which repos could the fleet onboard right now'."""
    token, _ = mint_installation_token(installation_id)
    repos: list[dict] = []
    page = 1
    while True:
        _status, data = _request(
            "GET",
            f"{GITHUB_API_BASE}/installation/repositories?per_page=100&page={page}",
            token=token,
        )
        batch = data.get("repositories", [])
        for r in batch:
            repos.append(
                {"repo": r.get("full_name", ""), "private": bool(r.get("private"))}
            )
        if len(batch) < 100:
            break
        page += 1
    return repos


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
    # GitHub returns UTC ("...Z"), so convert with calendar.timegm (UTC) — NOT
    # time.mktime, which assumes local time and would skew the cached expiry on
    # any host whose TZ isn't UTC (re-minting every call, or worse, over-caching).
    try:
        exp_epoch = calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        exp_epoch = now + 3300
    _token_cache[installation_id] = (token, exp_epoch)
    return token, expires_at



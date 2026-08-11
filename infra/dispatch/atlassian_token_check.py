"""Atlassian API-token expiry checker (atlassian-connector spec §A6.2/§A9.1).

A small scheduled Lambda that scans the ``atlassian_site#`` rows and, for any
site whose ``token_expires_at`` falls within a warning window (14 / 3 / 0 days
out), fans a ``credential_expired`` error-tier notification to subscribed
channels. Atlassian API tokens expire (max 1 year), and a silently-expired token
turns every ticket/page read and every agent reply into an auth failure — this
surfaces it before that happens.

Best-effort + idempotent: it only READS site rows and emits notifications; it
never rotates or writes a token. Runs daily.
"""

import logging
import time

import config_query
import notify

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DAY = 86400
# Days-out thresholds at which we warn (14 / 3 / 0). A site whose remaining
# lifetime crosses one of these buckets on a given day gets one notification.
_WARN_DAYS = (14, 3, 0)


def _bucket(seconds_left: float) -> int | None:
    """The smallest warning threshold (in days) that ``seconds_left`` is at or
    under, or None if the token is comfortably in the future / has no expiry."""
    days_left = seconds_left / _DAY
    for threshold in sorted(_WARN_DAYS):  # 0, 3, 14
        if days_left <= threshold:
            return threshold
    return None


def handler(event, context=None):
    now = time.time()
    checked = warned = 0
    for site in config_query.query_kind("atlassian_site"):
        if not site.get("enabled") or site.get("status") != "active":
            continue
        checked += 1
        expires_at = site.get("token_expires_at")
        if not expires_at:
            continue  # unknown expiry (classic unscoped token) — nothing to warn
        seconds_left = float(expires_at) - now
        bucket = _bucket(seconds_left)
        if bucket is None:
            continue
        site_name = site.get("site_name") or site.get("site_id", "")
        when = "has EXPIRED" if seconds_left <= 0 else f"expires in ~{bucket} day(s)"
        try:
            notify.notify(
                tier=notify.TIER_ERROR,
                event="credential_expired",
                text=f"🔑 The Atlassian API token for *{site_name}* {when}. "
                     "Mint a new scoped token and re-connect the site in "
                     "Connectors → Atlassian, or agents will lose Jira/Confluence access.",
            )
            warned += 1
        except Exception:  # noqa: BLE001
            logger.exception("credential_expired notify failed for %s", site.get("site_id"))
    logger.info("atlassian token check: %d active sites, %d warned", checked, warned)
    return {"checked": checked, "warned": warned}

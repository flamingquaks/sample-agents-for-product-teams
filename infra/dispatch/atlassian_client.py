"""Shared Atlassian REST + credential helpers for the Jira/Confluence brokers,
receivers, and reply path (atlassian-connector spec §A6).

ONE service-account API token per site (SSM SecureString), fetched per
invocation — never a module global (threat T-8/T-36/T-51). Basic auth to the
Atlassian Cloud REST APIs (v3 for Jira, v1/v2 for Confluence). This module owns:

  - the per-site token fetch (from the ``atlassian_site#`` row's derived SSM
    path) and the Basic-auth header,
  - a thin REST caller returning ``(status, json)``,
  - the ADF ⇄ plain-text walker shared by the receivers (mention scan, comment
    front-loading — §B1.1/§C1.1) and the reply path (``_text_to_adf``),
  - the Markdown ⇄ Confluence storage-format converter (macro-free, XML-escaped —
    §C2.2, threat T-54), so no model-authored text ever becomes raw storage XHTML.

Credential model: the site token is site-WIDE (Atlassian tokens can't express
container scope), which is exactly why the brokers enforce the project/space
allowlist themselves (§A4 layer 4). This module just fetches + calls; scoping is
the brokers' job.
"""

import base64
import html
import logging
import os
import re
import time
from xml.sax.saxutils import escape as _xml_escape

import boto3
import requests

logger = logging.getLogger(__name__)

_ssm = boto3.client("ssm")
_ddb = None

_TIMEOUT = 15


class AtlassianError(Exception):
    """A credential/config failure reaching Atlassian (not a REST 4xx — those
    come back as structured tool results the agent can react to)."""


# --- site row + token --------------------------------------------------------


def _config_table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(os.environ["FLEET_CONFIG_TABLE"])
    return _ddb


def get_site(site_id: str) -> dict | None:
    """The ``atlassian_site#`` row for ``site_id``, or None. Direct key get (the
    pk is a known constant) — the brokers/receivers read one site per call."""
    if not site_id:
        return None
    resp = _config_table().get_item(Key={"pk": f"atlassian_site#{site_id}"})
    return resp.get("Item")


def pin_forge_app_id(site: dict, claims: dict) -> bool:
    """Pin the delivering Forge app id onto the site row the first time a
    delivery verifies (§A5/T-54), shared by both receivers.

    Conditioned on the row still having no pinned id, so a later forged delivery
    can't overwrite the pin. Mutates ``site`` in place so the same request's
    downstream checks see the value. Returns True if a pin was written.

    A token that carries NO ``app`` claim is a hard problem, not a silent no-op:
    the pin can never engage, which would leave the app-id check permanently
    disabled — so we log a WARNING (distinct from the normal 'not yet pinned'
    state) and emit a metric the operator can alarm on. We do NOT pin a placeholder
    (that would defeat the check); the site stays unpinned and the operator must
    investigate why Atlassian's FIT lacks an app claim."""
    import mentions  # lazy: avoids any receiver/import-order coupling

    app_id = mentions.forge_app_id_from_claims(claims)
    if not app_id:
        logger.warning(
            "Forge app id absent from a verified FIT for site %s — cannot pin; "
            "the app-id check stays disabled for this site until a token carries "
            "an 'app' claim. Investigate the Forge deploy / token shape.",
            site.get("site_id"),
        )
        _emit_pin_absent_metric(site.get("site_id", ""))
        return False
    try:
        _config_table().update_item(
            Key={"pk": f"atlassian_site#{site['site_id']}"},
            UpdateExpression="SET forge_app_id = :a, updated_at = :u",
            ConditionExpression="attribute_exists(pk) AND "
            "(attribute_not_exists(forge_app_id) OR forge_app_id = :empty)",
            ExpressionAttributeValues={
                ":a": app_id, ":u": int(time.time()), ":empty": "",
            },
        )
        site["forge_app_id"] = app_id
        logger.info("pinned forge app id for site %s", site["site_id"])
        return True
    except Exception:  # noqa: BLE001 — a concurrent pin / race is fine (idempotent)
        logger.info("forge app id pin skipped for %s (already pinned?)", site.get("site_id"))
        return False


def _emit_pin_absent_metric(site_id: str) -> None:
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace=os.environ.get("CLOUDWATCH_NAMESPACE", "SDLCAgents/Dispatch"),
            MetricData=[{
                "MetricName": "ForgeAppIdAbsent",
                "Dimensions": [{"Name": "Stage", "Value": os.environ.get("STAGE", "dev")}],
                "Value": 1.0, "Unit": "Count",
            }],
        )
    except Exception:  # noqa: BLE001 — metrics are best-effort
        logger.debug("could not emit ForgeAppIdAbsent metric for %s", site_id)


def _token_param(stage: str, site_id: str) -> str:
    return f"/sdlc-agents/{stage}/atlassian/{site_id}/api-token"


def fetch_token(site: dict) -> str:
    """Fetch a site's service-account API token from SSM (per invocation). Raises
    AtlassianError when the parameter is missing/empty (fail closed)."""
    param = site.get("api_token_param") or _token_param(
        os.environ.get("STAGE", "dev"), site.get("site_id", "")
    )
    try:
        resp = _ssm.get_parameter(Name=param, WithDecryption=True)
    except Exception as exc:  # noqa: BLE001
        raise AtlassianError(f"could not read Atlassian token {param}") from exc
    value = resp.get("Parameter", {}).get("Value")
    if not value:
        raise AtlassianError(f"Atlassian token {param} is empty")
    return value


def _basic_auth(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def rest(
    site: dict,
    method: str,
    path: str,
    token: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    """Call an Atlassian Cloud REST endpoint (``path`` relative to the site url)
    and return ``(status, json)``. Basic auth with the service account's email +
    token. Never logs the token or the response body (either can carry
    credential/PII material)."""
    email = site.get("bot_email") or ""
    url = f"{site['site_url'].rstrip('/')}{path}"
    resp = requests.request(
        method,
        url,
        headers={
            "Authorization": _basic_auth(email, token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        params=params or None,
        json=body if body is not None else None,
        timeout=_TIMEOUT,
    )
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    return resp.status_code, data


# --- ADF (Atlassian Document Format) walker ----------------------------------
# Jira comment bodies and Confluence inline/footer comments are ADF — a nested
# node tree. We DON'T regex the JSON (the spec is explicit, §B1.1): a proper
# walker flattens text and finds mention nodes by structure.


def adf_to_text(node) -> str:
    """Flatten an ADF node tree to plain text. ``text`` nodes contribute their
    text; ``hardBreak``/``paragraph`` add newlines; everything else recurses over
    ``content``. A mention node contributes ``@<display>`` so the flattened text
    still reads naturally for the fallback resolver."""
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)
    ntype = node.get("type")
    if ntype == "text":
        return str(node.get("text", ""))
    if ntype == "mention":
        attrs = node.get("attrs") or {}
        label = attrs.get("text") or attrs.get("displayName") or ""
        # Atlassian's mention ``text`` attr already carries a leading "@"; don't
        # double it. Fall back to prefixing a bare display name.
        return label if label.startswith("@") else f"@{label}"
    if ntype == "hardBreak":
        return "\n"
    inner = adf_to_text(node.get("content"))
    if ntype in ("paragraph", "heading", "listItem", "blockquote"):
        return inner + "\n"
    return inner


def adf_mention_account_ids(node) -> list[str]:
    """Every account id referenced by a ``mention`` node in an ADF tree, in
    document order. The receiver checks whether the fleet bot's account id is
    among them (the explicit-mention signal, §B1.1)."""
    found: list[str] = []

    def walk(n):
        if isinstance(n, list):
            for item in n:
                walk(item)
            return
        if not isinstance(n, dict):
            return
        if n.get("type") == "mention":
            aid = (n.get("attrs") or {}).get("id")
            if aid:
                found.append(str(aid))
        walk(n.get("content"))

    walk(node)
    return found


def text_to_adf(text: str) -> dict:
    """Wrap plain text in a minimal ADF document (§B1.3) — one paragraph per
    line. Used by the reply path to post acks / rejects / onboarding notices as
    Jira comments. Text only: no mentions, no macros, no injection surface."""
    lines = (text or "").split("\n")
    content = []
    for line in lines:
        para = {"type": "paragraph", "content": []}
        if line:
            para["content"].append({"type": "text", "text": line})
        content.append(para)
    return {"type": "doc", "version": 1, "content": content or [
        {"type": "paragraph", "content": []}
    ]}


# --- Markdown ⇄ Confluence storage format (§C2.2, T-54) -----------------------
# Bodies cross the tool boundary as MARKDOWN; the broker owns the conversion so
# the model never authors raw storage XHTML. Macro-free subset, all output
# XML-escaped — no macro/XML injection surface.

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline_md_to_storage(text: str) -> str:
    """Convert inline markdown in ONE already-XML-escaped line to storage tags.
    Order matters: code spans first (so their content isn't re-marked), then
    links, bold, italic. The input is escaped BEFORE this so no raw ``<`` from
    the model survives — the only tags in the output are the ones we emit."""
    # Links: [label](url) — the url is attribute-escaped; only http(s) allowed.
    def _link(m):
        label, url = m.group(1), m.group(2)
        if not re.match(r"^https?://", url):
            return label  # drop non-http links to plain text (no javascript: etc.)
        return f'<a href="{url}">{label}</a>'

    text = _MD_LINK_RE.sub(_link, text)
    text = _MD_CODE_RE.sub(r"<code>\1</code>", text)
    text = _MD_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def markdown_to_storage(md: str) -> str:
    """Convert a markdown body to Confluence storage format (a macro-free XHTML
    subset). Every source character is XML-escaped FIRST (so model text can't
    inject a tag or a macro), then a small set of block/inline constructs
    (headings, paragraphs, bullet/numbered lists, code fences, bold/italic/code/
    links) is re-expressed as storage tags. Anything unrecognized stays as an
    escaped paragraph. No ``<ac:*>`` macros are ever emitted (T-54)."""
    out: list[str] = []
    lines = (md or "").split("\n")
    i = 0
    ul: list[str] = []
    ol: list[str] = []

    def _flush_lists():
        if ul:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in ul) + "</ul>")
            ul.clear()
        if ol:
            out.append("<ol>" + "".join(f"<li>{x}</li>" for x in ol) + "</ol>")
            ol.clear()

    while i < len(lines):
        raw = lines[i]
        # Fenced code block — content is escaped verbatim, no inline marking.
        if raw.strip().startswith("```"):
            _flush_lists()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(_xml_escape(lines[i]))
                i += 1
            i += 1  # closing fence
            out.append(f'<ac:structured-macro ac:name="noformat"><ac:plain-text-body>'
                       f'<![CDATA[{chr(10).join(_unescape_cdata(b) for b in buf)}]]>'
                       f'</ac:plain-text-body></ac:structured-macro>')
            continue
        line = _xml_escape(raw)
        heading = _MD_HEADING_RE.match(line)
        if heading:
            _flush_lists()
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline_md_to_storage(heading.group(2))}</h{level}>")
            i += 1
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if bullet:
            if ol:
                _flush_lists()
            ul.append(_inline_md_to_storage(bullet.group(1)))
            i += 1
            continue
        if numbered:
            if ul:
                _flush_lists()
            ol.append(_inline_md_to_storage(numbered.group(1)))
            i += 1
            continue
        _flush_lists()
        if line.strip():
            out.append(f"<p>{_inline_md_to_storage(line)}</p>")
        i += 1
    _flush_lists()
    return "".join(out)


def _unescape_cdata(escaped: str) -> str:
    """Inside a CDATA block the raw text is literal — undo the XML escaping we
    applied line-by-line so a code fence shows the real characters (CDATA itself
    prevents markup interpretation; we only guard against a literal ``]]>``)."""
    return html.unescape(escaped).replace("]]>", "]]&gt;")


def storage_to_markdown(storage: str) -> str:
    """Best-effort storage-format → markdown for ``get_page`` (§C2.2). Strips
    tags to readable text with a few structural conversions; the agent reads
    pages as markdown and writes them back as markdown, so a lossy round-trip is
    acceptable (the version history is the source of truth, not our render)."""
    if not storage:
        return ""
    text = storage
    text = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "#" * int(m.group(1)) + " " + m.group(2) + "\n", text, flags=re.S)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.S)
    text = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", text, flags=re.S)
    text = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", text, flags=re.S)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.S)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.S)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)  # strip any remaining tags
    return html.unescape(text).strip()


def now_epoch() -> int:
    return int(time.time())

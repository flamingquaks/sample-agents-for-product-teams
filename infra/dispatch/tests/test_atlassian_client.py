"""Unit tests for atlassian_client — the ADF walker + Markdown⇄storage converter.

Pure functions, no AWS/network. The security-load-bearing property is that no
model-authored text can inject a storage-format macro or raw XHTML tag (T-54):
every source char is XML-escaped before any tag we emit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atlassian_client as ac  # noqa: E402


def test_adf_to_text_flattens_and_names_mentions():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "hey "},
            {"type": "mention", "attrs": {"id": "acc1", "text": "@SDLC Agents"}},
            {"type": "text", "text": " ship it"},
        ]},
    ]}
    flat = ac.adf_to_text(doc)
    assert "hey" in flat and "ship it" in flat and "@@SDLC Agents" not in flat


def test_adf_mention_account_ids():
    doc = {"content": [{"type": "mention", "attrs": {"id": "bot123"}},
                       {"type": "mention", "attrs": {"id": "user9"}}]}
    assert ac.adf_mention_account_ids(doc) == ["bot123", "user9"]


def test_text_to_adf_roundtrips_plain_text():
    adf = ac.text_to_adf("line one\nline two")
    assert adf["type"] == "doc"
    assert ac.adf_to_text(adf).strip() == "line one\nline two"


def test_markdown_to_storage_escapes_injection():
    # A model trying to inject a macro or a raw tag must be neutralized.
    md = 'Hello <script>alert(1)</script> & <ac:structured-macro/>'
    out = ac.markdown_to_storage(md)
    assert "<script>" not in out
    assert "<ac:structured-macro/>" not in out  # the model's literal macro is escaped
    assert "&lt;script&gt;" in out and "&amp;" in out


def test_markdown_to_storage_headings_and_bold():
    out = ac.markdown_to_storage("# Title\n\nSome **bold** and `code`.")
    assert "<h1>Title</h1>" in out
    assert "<strong>bold</strong>" in out and "<code>code</code>" in out


def test_markdown_to_storage_bullets():
    out = ac.markdown_to_storage("- one\n- two")
    assert out.count("<li>") == 2 and "<ul>" in out


def test_markdown_link_only_http():
    out = ac.markdown_to_storage("[click](javascript:alert(1)) and [ok](https://x.com)")
    assert "javascript:" not in out           # dropped to plain text
    assert '<a href="https://x.com">ok</a>' in out


def test_storage_to_markdown_readable():
    md = ac.storage_to_markdown("<h2>Heading</h2><p>Para with <strong>bold</strong></p>")
    assert "## Heading" in md and "**bold**" in md


# --- shared Forge app-id pin (§A5/T-54) --------------------------------------

import os  # noqa: E402

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

_SITE = "33333333-3333-3333-3333-333333333333"


def _pin_table():
    ddb = boto3.client("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="fleet-config-pin", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )
    t = boto3.resource("dynamodb", region_name="us-west-2").Table("fleet-config-pin")
    t.put_item(Item={"pk": f"atlassian_site#{_SITE}", "site_id": _SITE, "forge_app_id": ""})
    return t


@mock_aws
def test_pin_forge_app_id_writes_and_mutates(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("FLEET_CONFIG_TABLE", "fleet-config-pin")
    ac._ddb = None
    t = _pin_table()
    site = {"site_id": _SITE, "forge_app_id": ""}
    assert ac.pin_forge_app_id(site, {"app": {"id": "ari:app/REAL"}}) is True
    assert site["forge_app_id"] == "ari:app/REAL"  # mutated in place
    row = t.get_item(Key={"pk": f"atlassian_site#{_SITE}"})["Item"]
    assert row["forge_app_id"] == "ari:app/REAL"


@mock_aws
def test_pin_forge_app_id_absent_claim_does_not_pin(monkeypatch):
    # Fix #4: a token with NO app claim must NOT silently no-op into a permanent
    # unpinned state — it returns False (so the caller/operator can tell) and
    # leaves the row unpinned rather than writing a placeholder.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("FLEET_CONFIG_TABLE", "fleet-config-pin")
    ac._ddb = None
    t = _pin_table()
    site = {"site_id": _SITE, "forge_app_id": ""}
    assert ac.pin_forge_app_id(site, {"cloudId": _SITE}) is False  # no 'app' claim
    assert site.get("forge_app_id") == ""
    row = t.get_item(Key={"pk": f"atlassian_site#{_SITE}"})["Item"]
    assert row["forge_app_id"] == ""  # not pinned with a placeholder


@mock_aws
def test_pin_forge_app_id_does_not_overwrite_existing(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("FLEET_CONFIG_TABLE", "fleet-config-pin")
    ac._ddb = None
    t = _pin_table()
    t.put_item(Item={"pk": f"atlassian_site#{_SITE}", "site_id": _SITE,
                     "forge_app_id": "ari:app/ORIGINAL"})
    site = {"site_id": _SITE, "forge_app_id": ""}
    # A conditional write: an already-pinned row is not overwritten (returns False).
    assert ac.pin_forge_app_id(site, {"app": {"id": "ari:app/EVIL"}}) is False
    row = t.get_item(Key={"pk": f"atlassian_site#{_SITE}"})["Item"]
    assert row["forge_app_id"] == "ari:app/ORIGINAL"

"""Schema-lint for infra/slack-app-manifest.yaml.

The manifest is hand-authored YAML handed straight to Slack, so shape mistakes
(over-long description, invalid feature keys, wrong suggested_prompts shape)
only surfaced at app-creation time during real deploys. This test encodes the
documented constraints from https://docs.slack.dev/reference/app-manifest so
those mistakes fail locally instead.

Constraints are intentionally conservative — we assert the limits the docs
state and the keys we actually use; we do not try to mirror Slack's full schema.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

MANIFEST = Path(__file__).resolve().parents[3] / "infra" / "slack-app-manifest.yaml"

# Valid top-level `features` keys per the app-manifest reference.
VALID_FEATURE_KEYS = {
    "app_home", "agent_view", "assistant_view", "bot_user", "rich_previews",
    "search", "shortcuts", "slash_commands", "unfurl_domains", "workflow_steps",
}
VALID_TOP_LEVEL = {
    "_metadata", "app_directory", "display_information", "features",
    "oauth_config", "settings", "functions", "workflows", "datastores",
    "outgoing_domains", "types", "metadata_events", "external_auth_providers",
    "compliance", "mcp_servers",
}


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load(MANIFEST.read_text())


def test_top_level_keys_are_valid(manifest):
    bad = set(manifest) - VALID_TOP_LEVEL
    assert not bad, f"invalid top-level manifest keys: {bad}"


def test_display_information_limits(manifest):
    di = manifest["display_information"]
    assert len(di["name"]) <= 35, f"name is {len(di['name'])} chars (max 35)"
    assert len(di["description"]) <= 140, f"description is {len(di['description'])} chars (max 140)"
    assert len(di.get("long_description", "")) <= 4000, "long_description exceeds 4000 chars"


def test_feature_keys_are_valid(manifest):
    bad = set(manifest["features"]) - VALID_FEATURE_KEYS
    assert not bad, (
        f"invalid features keys: {bad}. Note the Agents surface is 'agent_view' "
        "or 'assistant_view' — NOT 'assistant'."
    )


def test_assistant_view_shape(manifest):
    # The fleet's receiver is built on the Assistant primitives (assistant:write
    # scope, assistant_thread_started event, assistant.threads.* APIs), which
    # pair with the `assistant_view` surface — NOT `agent_view` (a different
    # container that rejects assistant:write and needs separate enablement).
    feats = manifest["features"]
    assert "agent_view" not in feats, (
        "use assistant_view, not agent_view: agent_view rejects the "
        "assistant:write scope this app uses ('Agent View should not use "
        "assistant:write bot scope')."
    )
    # Assert the surface is actually PRESENT — not just that the wrong one is
    # absent. A manifest with neither would otherwise pass silently.
    assert "assistant_view" in feats, "manifest must declare features.assistant_view"
    av = feats["assistant_view"]
    assert "assistant_description" in av, "assistant_view requires assistant_description"
    for prompt in av.get("suggested_prompts", []):
        assert set(prompt) == {"title", "message"}, (
            f"suggested_prompts entries must have exactly title+message, got {set(prompt)}"
        )


def test_slash_commands_shape(manifest):
    for sc in manifest["features"].get("slash_commands", []):
        assert sc["command"].startswith("/"), f"{sc['command']} must start with /"
        assert len(sc["command"]) <= 32, f"{sc['command']} exceeds 32 chars"
        assert len(sc["description"]) <= 2000, f"{sc['command']} description too long"
        assert len(sc.get("usage_hint", "")) <= 1000, f"{sc['command']} usage_hint too long"


def test_oauth_and_events_present(manifest):
    # The Assistant capability needs the assistant:write scope + the
    # assistant_thread_started event in addition to the assistant_view block.
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert "assistant:write" in scopes
    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "assistant_thread_started" in events


def test_interactivity_has_handler_or_disabled(manifest):
    # If interactivity is enabled, the receiver must have an interactions
    # handler — otherwise interactive payloads fall into the slash-command path
    # and error. We currently ship no such handler, so it must be disabled.
    interactivity = manifest["settings"].get("interactivity", {})
    assert interactivity.get("is_enabled") is False, (
        "interactivity is enabled but the receiver has no interactions handler; "
        "disable it or add a /slack/interactions route + handler first."
    )

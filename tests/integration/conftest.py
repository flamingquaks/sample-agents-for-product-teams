"""Shared fixtures for Slack and Discord integration tests.

These tests drive real bot endpoints in sandbox environments. Configure via
environment variables or a .env file at the repo root:

  SLACK_BOT_TOKEN          — xoxb-* bot token for the sandbox Slack workspace
  SLACK_SIGNING_SECRET     — signing secret for the sandbox Slack app
  SLACK_TEST_CHANNEL       — channel ID to post test messages in
  DISCORD_BOT_TOKEN        — bot token for the sandbox Discord application
  DISCORD_PUBLIC_KEY        — hex-encoded public key for the sandbox app
  DISCORD_APPLICATION_ID   — snowflake application ID
  DISCORD_TEST_CHANNEL     — channel ID for test messages
  DISCORD_TEST_GUILD       — guild (server) ID for slash-command propagation
  WEBHOOK_BASE_URL         — base URL of the deployed API Gateway (or local)
  AWS_REGION               — region where the stack is deployed (for SSM reads)
  STAGE                    — dev|staging|prod (defaults to dev)

Tests are skipped (not failed) when credentials are missing, so CI can run
the full suite without sandbox secrets in non-integration runs.
"""

import os

import pytest

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_or_skip(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"{name} not set — skipping integration test")
    return val


@pytest.fixture
def slack_bot_token():
    return _env_or_skip("SLACK_BOT_TOKEN")


@pytest.fixture
def slack_signing_secret():
    return _env_or_skip("SLACK_SIGNING_SECRET")


@pytest.fixture
def slack_test_channel():
    return _env_or_skip("SLACK_TEST_CHANNEL")


@pytest.fixture
def discord_bot_token():
    return _env_or_skip("DISCORD_BOT_TOKEN")


@pytest.fixture
def discord_public_key():
    return _env_or_skip("DISCORD_PUBLIC_KEY")


@pytest.fixture
def discord_application_id():
    return _env_or_skip("DISCORD_APPLICATION_ID")


@pytest.fixture
def discord_test_channel():
    return _env_or_skip("DISCORD_TEST_CHANNEL")


@pytest.fixture
def discord_test_guild():
    return _env_or_skip("DISCORD_TEST_GUILD")


@pytest.fixture
def webhook_base_url():
    return _env_or_skip("WEBHOOK_BASE_URL")


@pytest.fixture
def stage():
    return os.environ.get("STAGE", "dev")

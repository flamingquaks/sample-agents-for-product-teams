"""Set up a Slack workspace for the fleet: emit the app manifest + store the
per-workspace signing secret and bot token.

Multi-workspace: each Slack workspace the fleet serves has its own signing
secret (verify inbound requests) and bot token (post replies), stored as SSM
SecureStrings under ``/sdlc-agents/<stage>/slack/<team_id>/{signing-secret,bot-token}``.
The Slack receiver Lambda reads them at request time; it never holds
``ssm:PutParameter`` (parity with the Asana webhook secret, threat T-9), so an
operator runs this script to place them.

Two steps:
  1. ``manifest`` — print the Slack app manifest (scopes + event subscription +
     slash commands) to paste into api.slack.com/apps → Create from manifest.
     After creating + installing the app, copy its Signing Secret and Bot Token.
  2. ``store`` — write the app-level signing secret (once per app) and the
     per-workspace bot token to SSM.

The **signing secret is app-level** (one per Slack app; verifies every inbound
request incl. the team-less url_verification handshake), stored at
``/sdlc-agents/<stage>/slack/signing-secret``. The **bot token is
per-workspace** (posts replies into that workspace), stored at
``/sdlc-agents/<stage>/slack/<team_id>/bot-token``.

Usage:
    python scripts/bootstrap_slack.py manifest \\
        --webhook-base https://abc.execute-api.us-west-2.amazonaws.com/dev

    python scripts/bootstrap_slack.py store \\
        --stage dev --region us-west-2 --team-id T0ACME12 \\
        --signing-secret <secret> --bot-token <xoxb-...>

Onboard the workspace in the dashboard Connectors → Slack panel (records the
team id + channel policy); this script only handles the secrets Slack won't let
the app read for itself.
"""

import argparse
import json
import sys

# The slash command that files a channel-onboarding request (matches the
# receiver's ONBOARD_COMMAND default).
ONBOARD_COMMAND = "sdlc-onboard-channel"


def build_manifest(webhook_base: str, app_name: str = "SDLC Agent Fleet") -> dict:
    """The Slack app manifest. ``webhook_base`` is the deployed webhook API base
    (…/slack/events + …/slack/commands hang off it). Scopes are minimal: read
    mentions, post replies, run slash commands, resolve a user's email for
    email-based grant rules."""
    base = webhook_base.rstrip("/")
    return {
        "display_information": {"name": app_name},
        "features": {
            "bot_user": {"display_name": "fleet", "always_online": True},
            "slash_commands": [
                {
                    "command": f"/{ONBOARD_COMMAND}",
                    "url": f"{base}/slack/commands",
                    "description": "Request this channel be onboarded for fleet agents",
                    "usage_hint": "[agent ...]",
                    "should_escape": False,
                },
                {
                    "command": "/fleet",
                    "url": f"{base}/slack/commands",
                    "description": "Dispatch a fleet agent",
                    "usage_hint": "@agent your instruction",
                    "should_escape": False,
                },
            ],
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "chat:write",
                    "commands",
                    "users:read",
                    "users:read.email",
                ]
            }
        },
        "settings": {
            "event_subscriptions": {
                "request_url": f"{base}/slack/events",
                "bot_events": ["app_mention"],
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
        },
    }


def _store(stage: str, region: str, team_id: str, signing_secret: str, bot_token: str) -> None:
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    # Signing secret is APP-level (one per app); bot token is per-workspace.
    params = {
        f"/sdlc-agents/{stage}/slack/signing-secret": signing_secret,
        f"/sdlc-agents/{stage}/slack/{team_id}/bot-token": bot_token,
    }
    for name, value in params.items():
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
        print(f"stored {name}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="print the Slack app manifest")
    m.add_argument("--webhook-base", required=True)
    m.add_argument("--app-name", default="SDLC Agent Fleet")

    s = sub.add_parser("store", help="store a workspace's secrets in SSM")
    s.add_argument("--stage", required=True)
    s.add_argument("--region", default=None)
    s.add_argument("--team-id", required=True)
    s.add_argument("--signing-secret", required=True)
    s.add_argument("--bot-token", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "manifest":
        print(json.dumps(build_manifest(args.webhook_base, args.app_name), indent=2))
        return 0
    _store(args.stage, args.region, args.team_id, args.signing_secret, args.bot_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())

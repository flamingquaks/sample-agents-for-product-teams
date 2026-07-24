"""Slack app manifest builder for the admin API (spec §10).

Parallels the GitHub-App manifest flow (`github_client.generate_manifest`): the
dashboard hands an admin the ready-to-paste Slack app manifest so they can
create the app from the UI instead of running `scripts/bootstrap_slack.py` by
hand. The manifest is IDENTICAL in shape to the CLI's — the two are kept in sync
deliberately (this is the server-side copy the UI serves; the CLI copy is for
operators without dashboard access). Storing the resulting signing secret + bot
token is still an out-of-band operator step (the admin Lambda does not hold
`ssm:PutParameter` on the Slack secret paths — parity with T-9).
"""

# Slash commands + notify command names must match the receiver's defaults
# (slack_webhook.ONBOARD_COMMAND / NOTIFY_COMMAND / MESSAGE_COMMAND).
ONBOARD_COMMAND = "sdlc-onboard-channel"
NOTIFY_COMMAND = "sdlc-notify"
MESSAGE_COMMAND = "sdlc-message-agent"


def build_manifest(webhook_base: str, app_name: str = "SDLC Agent Fleet") -> dict:
    """The Slack app manifest. ``webhook_base`` is the deployed webhook API base
    (…/slack/events, …/slack/commands, …/slack/interactions hang off it).

    Kept byte-for-byte aligned with scripts/bootstrap_slack.build_manifest — the
    receiver, the CLI, and this admin endpoint must all agree on scopes, events,
    commands, and the interactivity URL."""
    base = webhook_base.rstrip("/")
    return {
        "display_information": {"name": app_name},
        "features": {
            "bot_user": {"display_name": "sdlc-agents", "always_online": True},
            "slash_commands": [
                {
                    "command": f"/{ONBOARD_COMMAND}",
                    "url": f"{base}/slack/commands",
                    "description": "Request this channel be onboarded for fleet agents",
                    "usage_hint": "opens a form to pick agents and repositories",
                    "should_escape": False,
                },
                {
                    "command": f"/{NOTIFY_COMMAND}",
                    "url": f"{base}/slack/commands",
                    "description": "Configure fleet notifications for this channel",
                    "usage_hint": "opens the notification settings form",
                    "should_escape": False,
                },
                {
                    "command": f"/{MESSAGE_COMMAND}",
                    "url": f"{base}/slack/commands",
                    "description": "Message a fleet agent (guided form)",
                    "usage_hint": "opens a form to pick an agent, repos, and message",
                    "should_escape": False,
                },
            ],
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "channels:join",
                    "channels:read",
                    "chat:write",
                    "chat:write.customize",
                    "commands",
                    "groups:read",
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
            "interactivity": {
                "is_enabled": True,
                "request_url": f"{base}/slack/interactions",
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
        },
    }

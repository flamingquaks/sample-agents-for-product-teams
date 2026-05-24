"""Bootstrap a Discord application for the SDLC Agent Fleet.

Operator-run, idempotent. Workflow:

    1. Operator creates a Discord application at
       https://discord.com/developers/applications and gets:
         - Application (client) ID
         - Public key
         - Bot token
       The script prompts for all three interactively unless --register-only
       is passed (skip credential prompts, assume SSM already populated).

    2. Writes bot token to /sdlc-agents/discord-bot-token (SecureString) and
       public key to /sdlc-agents/discord-public-key (String).

    3. Registers slash commands via PUT /applications/{app_id}/commands.
       One command per agent in .dispatch/agents.yaml that has discord triggers.
       Each command has a single required string option "instruction".

       Pass --guild-id for guild-scoped commands (instant propagation, good for
       dev/testing). Omit for global commands (up to 1 hour to propagate).

    4. Prints the Interactions Endpoint URL from the CloudFormation stack
       output DiscordInteractionsEndpoint (paste into Developer Portal).

    5. Prints the bot install URL with scopes:
         applications.commands, bot
       and permissions:
         Send Messages (2048) + Read Message History (65536) = 67584

Usage:

    python scripts/bootstrap_discord_app.py \\
        --stage dev \\
        --region us-west-2 \\
        --app-id 1234567890123456789

    # Dev/guild-scoped (commands available immediately):
    python scripts/bootstrap_discord_app.py \\
        --stage dev \\
        --region us-west-2 \\
        --app-id 1234567890123456789 \\
        --guild-id 9876543210987654321

    # Re-register commands only (skip credential prompts):
    python scripts/bootstrap_discord_app.py \\
        --stage dev \\
        --region us-west-2 \\
        --app-id 1234567890123456789 \\
        --register-only

Requires operator AWS credentials with ssm:PutParameter on the two Discord
SSM parameters. A bot token read is not needed to register commands -- the
script uses a bot token only for the PUT /applications/{app_id}/commands call.
"""

import argparse
import getpass
import json
import sys

import boto3
import requests
import yaml
from botocore.exceptions import ClientError

DISCORD_API = "https://discord.com/api/v10"  # also defined in infra/dispatch/reply.py and agents/shared/discord_post.py
DISCORD_BOT_TOKEN_PARAM = "/sdlc-agents/discord-bot-token"  # nosec B105 -- SSM param name
DISCORD_PUBLIC_KEY_PARAM = "/sdlc-agents/discord-public-key"

# Bot install permissions bitfield: Send Messages (2048) + Read Message History (65536)
BOT_PERMISSIONS = 2048 + 65536

AGENTS_YAML_PATH = ".dispatch/agents.yaml"


def load_discord_agents(agents_yaml_path: str) -> list[str]:
    """Return agent names from .dispatch/agents.yaml that have discord triggers."""
    try:
        with open(agents_yaml_path) as f:
            registry = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Warning: {agents_yaml_path} not found. Registering default commands.", file=sys.stderr)
        return ["workitems", "docwriter"]

    agents = []
    for name, config in (registry.get("agents") or {}).items():
        triggers = config.get("triggers", {})
        if "discord" in triggers and triggers["discord"]:
            agents.append(name)
    return agents or ["workitems", "docwriter"]


def store_ssm_param(ssm, name: str, value: str, param_type: str, overwrite: bool = True) -> None:
    """Write a parameter to SSM. Raises ClientError on permission failure."""
    ssm.put_parameter(
        Name=name,
        Value=value,
        Type=param_type,
        Overwrite=overwrite,
    )
    print(f"Stored {name} ({param_type})")


def build_command_body(agent_name: str) -> dict:
    """Build the Discord slash command definition for a single agent."""
    descriptions = {
        "workitems": "Project management: status reports, work decomposition, risk detection",
        "docwriter": "Documentation: API docs, user guides, release notes",
        "researcher": "Business analysis: research synthesis, competitive intel, backlog analysis",
        "adr": "ADR linker: tags issues and reviews PRs against the ADR library",
    }
    description = descriptions.get(agent_name, f"SDLC agent: {agent_name}")[:100]
    return {
        "name": agent_name,
        "type": 1,  # CHAT_INPUT
        "description": description,
        "options": [
            {
                "name": "instruction",
                "description": "What do you want the agent to do?",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    }


def register_commands(bot_token: str, app_id: str, agent_names: list[str], guild_id: str | None) -> None:
    """Register slash commands via Discord REST API (idempotent PUT)."""
    commands = [build_command_body(name) for name in agent_names]

    if guild_id:
        url = f"{DISCORD_API}/applications/{app_id}/guilds/{guild_id}/commands"
        scope = f"guild {guild_id} (instant propagation)"
    else:
        url = f"{DISCORD_API}/applications/{app_id}/commands"
        scope = "global (up to 1 hour to propagate)"

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    resp = requests.put(url, json=commands, headers=headers, timeout=30)
    if not resp.ok:
        print(f"Command registration failed: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()

    registered = resp.json()
    print(f"Registered {len(registered)} commands ({scope}):")
    for cmd in registered:
        print(f"  /{cmd['name']} — {cmd.get('description', '')[:60]}")


def get_interactions_endpoint(cfn, stack_name: str) -> str | None:
    """Look up DiscordInteractionsEndpoint from the CloudFormation stack."""
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        for output in stack.get("Outputs", []):
            if output["OutputKey"] == "DiscordInteractionsEndpoint":
                return output["OutputValue"]
    except ClientError as exc:
        print(f"Warning: could not read stack outputs ({exc})", file=sys.stderr)
    return None


def print_install_url(app_id: str) -> None:
    """Print the bot install URL with the minimum required scopes and permissions."""
    scopes = "applications.commands%20bot"
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={app_id}"
        f"&permissions={BOT_PERMISSIONS}"
        f"&scope={scopes}"
    )
    print(f"\nBot install URL (invite to your server):")
    print(f"  {url}")
    print(f"  Permissions: Send Messages ({2048}) + Read Message History ({65536}) = {BOT_PERMISSIONS}")
    print("  Scopes: applications.commands, bot")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", default="dev", help="e.g. dev, staging, prod")
    parser.add_argument("--region", default="us-west-2", help="AWS region of the fleet")
    parser.add_argument("--app-id", required=True, help="Discord application ID (snowflake)")
    parser.add_argument("--guild-id", default=None, help="Guild ID for dev/scoped commands (optional)")
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="Skip credential prompts; assume SSM is already populated. "
             "Re-registers slash commands only.",
    )
    parser.add_argument(
        "--agents-yaml",
        default=AGENTS_YAML_PATH,
        help=f"Path to agents registry (default: {AGENTS_YAML_PATH})",
    )
    args = parser.parse_args()

    stack_name = f"sdlc-agents-{args.stage}"
    session = boto3.Session(region_name=args.region)
    ssm = session.client("ssm")
    cfn = session.client("cloudformation")

    # --- Collect and store credentials --------------------------------------
    bot_token: str | None = None

    if args.register_only:
        print("--register-only: skipping credential prompts; reading bot token from SSM.")
        try:
            resp = ssm.get_parameter(Name=DISCORD_BOT_TOKEN_PARAM, WithDecryption=True)
            bot_token = resp["Parameter"]["Value"]
        except ClientError as exc:
            print(
                f"Error: bot token not found at {DISCORD_BOT_TOKEN_PARAM}: {exc}\n"
                "Run without --register-only to store it first.",
                file=sys.stderr,
            )
            return 1
    else:
        print("Discord application setup")
        print("=" * 40)
        print("Go to https://discord.com/developers/applications and:")
        print("  1. Select (or create) your application")
        print("  2. On the General Information page, copy the Public Key")
        print("  3. On the Bot page, copy the Bot Token (Reset Token if needed)")
        print()

        public_key = input("Paste the application Public Key: ").strip()
        bot_token = getpass.getpass("Paste the Bot Token (hidden): ").strip()

        if not public_key or not bot_token:
            print("Error: both public key and bot token are required.", file=sys.stderr)
            return 1

        print()
        print("Storing credentials in SSM...")
        try:
            store_ssm_param(ssm, DISCORD_PUBLIC_KEY_PARAM, public_key, "String")
            store_ssm_param(ssm, DISCORD_BOT_TOKEN_PARAM, bot_token, "SecureString")
        except ClientError as exc:
            print(f"SSM write failed: {exc}", file=sys.stderr)
            print(
                "Make sure your AWS credentials have ssm:PutParameter on "
                f"{DISCORD_BOT_TOKEN_PARAM} and {DISCORD_PUBLIC_KEY_PARAM}.",
                file=sys.stderr,
            )
            return 1

    # --- Register slash commands -------------------------------------------
    agent_names = load_discord_agents(args.agents_yaml)
    print(f"\nRegistering commands for agents: {agent_names}")
    try:
        register_commands(bot_token, args.app_id, agent_names, args.guild_id)
    except requests.RequestException as exc:
        print(f"Command registration failed: {exc}", file=sys.stderr)
        return 1

    # --- Print Interactions Endpoint URL ------------------------------------
    print()
    interactions_endpoint = get_interactions_endpoint(cfn, stack_name)
    if interactions_endpoint:
        print("Interactions Endpoint URL (paste into Discord Developer Portal):")
        print(f"  {interactions_endpoint}")
        print()
        print("Steps to complete setup in Discord Developer Portal:")
        print("  1. Open https://discord.com/developers/applications")
        print("  2. Select your application")
        print("  3. Go to General Information")
        print("  4. Paste the URL above into 'Interactions Endpoint URL'")
        print("  5. Click Save Changes -- Discord will send a PING to verify it")
    else:
        print(
            f"Warning: could not read DiscordInteractionsEndpoint from stack {stack_name}.\n"
            f"Deploy the foundation stack first (sam deploy), then re-run this script\n"
            f"or manually retrieve the URL from the CloudFormation stack outputs."
        )

    # --- Print bot install URL ---------------------------------------------
    print_install_url(args.app_id)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

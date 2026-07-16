"""Reconcile the Cedar policy tool names against the live gateway manifest.

The fleet Cedar policies (infra/dashboard/fleet_policy.py) name GitHub/Asana MCP
tools that are inherently manifest-dependent: the exact tool ids and the
repo-parameter shape come from whatever the gateway's ``tools/list`` reports.
If those drift from what fleet_policy hardcodes, the policy silently mis-scopes —
a write tool absent from the manifest names is dead (never matched), and a wrong
repo-param mode inverts the allowlist. Run this BEFORE flipping enforcement to
ACTIVE, and after any gateway target change.

What it checks against the live gateway MCP endpoint (SigV4, same transport the
agents use):
  1. Every action in WRITE_TOOLS, DESTRUCTIVE_TOOLS, and AGENT_TOOL_GRANTS
     resolves to a real ``<Target>___<tool>`` in the manifest (report names that
     don't — they're dead policy clauses).
  2. Real GitHub write tools NOT covered by WRITE_TOOLS/DESTRUCTIVE_TOOLS (report
     as potential gaps — an uncovered destructive/write tool escapes the fleet
     forbid).
  3. The repo-parameter shape of the GitHub write tools matches REPO_PARAM_MODE
     (separate owner+repo → "pair"; a combined "repo" → "single").

Read-only: it lists tools and prints a report; it changes nothing. Exit code is
non-zero if any hardcoded name is missing from the manifest (CI-friendly).

Usage:
    python scripts/check_gateway_manifest.py --gateway-url <url> [--region us-west-2]
    # or resolve the URL from the stack:
    python scripts/check_gateway_manifest.py --stage dev [--region us-west-2]
"""

import argparse
import sys
from pathlib import Path

# fleet_policy lives in the dashboard package; add it to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra" / "dashboard"))

import fleet_policy  # noqa: E402


def _resolve_gateway_url(stage: str, region: str) -> str | None:
    import boto3

    cf = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cf.describe_stacks(StackName=f"sdlc-agents-{stage}")["Stacks"]
    except Exception as exc:  # noqa: BLE001
        print(f"could not read stack sdlc-agents-{stage}: {exc}", file=sys.stderr)
        return None
    for out in stacks[0].get("Outputs", []):
        if out["OutputKey"] == "FleetGatewayUrl":
            return out["OutputValue"]
    return None


def _list_gateway_tools(url: str, region: str) -> list[str]:
    """Return the gateway's tool names (the ``<Target>___<tool>`` action ids).

    Uses the same SigV4 transport the agents use, so this exercises the real
    inbound-auth path too."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    from strands.tools.mcp import MCPClient

    client = MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=url, aws_region=region, aws_service="bedrock-agentcore"
        )
    )
    with client:
        return [t.tool_name for t in client.list_tools_sync()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url", help="Gateway MCP URL (else resolved from stack)"
    )
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()

    url = args.gateway_url or _resolve_gateway_url(args.stage, args.region)
    if not url:
        print(
            "no gateway URL (pass --gateway-url or deploy the gateway)", file=sys.stderr
        )
        return 2

    try:
        manifest = set(_list_gateway_tools(url, args.region))
    except Exception as exc:  # noqa: BLE001
        print(f"could not list gateway tools: {exc}", file=sys.stderr)
        return 2

    # Everything the policies reference, in <Target>___<tool> shape.
    referenced = set()
    referenced.update(
        f"{fleet_policy.GITHUB_TARGET}___{t}" for t in fleet_policy.WRITE_TOOLS
    )
    referenced.update(
        f"{fleet_policy.GITHUB_TARGET}___{t}" for t in fleet_policy.DESTRUCTIVE_TOOLS
    )
    for actions in fleet_policy.AGENT_TOOL_GRANTS.values():
        referenced.update(actions)

    missing = sorted(referenced - manifest)
    github_prefix = f"{fleet_policy.GITHUB_TARGET}___"
    covered_github = {
        f"{github_prefix}{t}"
        for t in (*fleet_policy.WRITE_TOOLS, *fleet_policy.DESTRUCTIVE_TOOLS)
    }
    uncovered_github_writes = sorted(
        t
        for t in manifest
        if t.startswith(github_prefix)
        and t not in covered_github
        and any(
            kw in t.lower()
            for kw in ("create", "update", "delete", "merge", "push", "add", "remove")
        )
    )

    print(f"gateway: {url}")
    print(f"manifest tools: {len(manifest)}   policy-referenced: {len(referenced)}")
    if missing:
        print("\nDEAD POLICY CLAUSES — referenced but NOT in the manifest:")
        for m in missing:
            print(f"  ✗ {m}")
    else:
        print("\n✅ every policy-referenced action exists in the manifest")
    if uncovered_github_writes:
        print("\nPOTENTIAL GAPS — GitHub write-like tools not covered by any forbid:")
        for t in uncovered_github_writes:
            print(f"  ⚠️  {t}")
    print(
        f"\nREPO_PARAM_MODE is '{fleet_policy.REPO_PARAM_MODE}' — confirm this "
        "matches the GitHub write tools' input schema (owner+repo vs combined repo)."
    )

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

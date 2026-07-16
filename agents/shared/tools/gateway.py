"""AgentCore Gateway routing for agent MCP tool access.

Agents connect to MCP servers one of two ways, chosen at runtime by the
``GATEWAY_MCP_URL`` env var:

- **Direct (default, GATEWAY_MCP_URL unset):** one MCPClient per vendor MCP
  server (GitHub, Asana), each with its own bearer token. This is the
  pre-gateway behavior; each agent builds those clients itself.

- **Gateway (GATEWAY_MCP_URL set):** a SINGLE MCPClient to the AgentCore
  Gateway, which fronts every target behind one MCP endpoint and enforces the
  Cedar policy engine. The gateway's inbound authorizer is AWS_IAM, so requests
  are SigV4-signed with the runtime's IAM role — NOT bearer tokens. We use
  ``mcp_proxy_for_aws.aws_iam_streamablehttp_client`` (AWS's documented transport
  for an IAM gateway) inside the same Strands ``MCPClient`` (verified against the
  Strands + Bedrock AgentCore docs). The runtime role needs
  ``bedrock-agentcore:InvokeGateway`` on the gateway ARN (granted by bootstrap).

In gateway mode the tool set is already aggregated + de-duplicated at the gateway
(and ``tools/list`` is filtered per principal by the policy engine), so the
per-server dedup the agents do in direct mode is unnecessary — one client's
``list_tools_sync()`` returns exactly the tools this agent's role may call.
"""

import os

from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client


def gateway_url() -> str | None:
    """The AgentCore Gateway MCP URL, or None when routing direct to vendors."""
    return os.environ.get("GATEWAY_MCP_URL") or None


def gateway_enabled() -> bool:
    return gateway_url() is not None


def build_gateway_client() -> MCPClient:
    """A single MCPClient to the AgentCore Gateway, SigV4-signed via the runtime
    role. Imports the AWS IAM transport lazily so direct-mode deploys don't need
    the extra dependency at import time."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    url = gateway_url()
    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-west-2"
    )
    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=url,
            aws_region=region,
            aws_service="bedrock-agentcore",
        )
    )


def build_bearer_client(url: str, token: str) -> MCPClient:
    """A direct-to-vendor MCPClient authenticated with a bearer token."""
    return MCPClient(
        lambda: streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"})
    )

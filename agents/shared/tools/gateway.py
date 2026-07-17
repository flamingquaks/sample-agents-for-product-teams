"""AgentCore Gateway routing for agent MCP tool access.

The fleet is **gateway-only**: every agent routes ALL MCP tool calls through the
AgentCore Gateway, which fronts every target behind one MCP endpoint, enforces
the Cedar policy engine, runs the SCM co-repo interceptor, and gives us
per-tool-call observability. There is no direct-to-vendor path — that would
bypass the policy engine, the interceptor, and the metrics we depend on.
``GATEWAY_MCP_URL`` is therefore REQUIRED; an agent refuses to start without it.

The gateway's inbound authorizer is AWS_IAM, so requests are SigV4-signed with
the runtime's IAM role — NOT bearer tokens. We use
``mcp_proxy_for_aws.aws_iam_streamablehttp_client`` (AWS's documented transport
for an IAM gateway) inside the Strands ``MCPClient``. The runtime role needs
``bedrock-agentcore:InvokeGateway`` on the gateway ARN (granted by bootstrap).

Per-dispatch trust: the agent stamps the **dispatch origin** and its **agent id**
as request headers (``x-dispatch-origin``, ``x-dispatch-agent``) when it builds
the client — runtime code that runs BEFORE the model, so the model can't forge
them (it controls tool arguments, never transport headers). The gateway's REQUEST
interceptor reads those headers to enforce co-repo grouping (which OTHER repos a
dispatch may act on) and per-agent access, then the SCM broker mints a GitHub App
token scoped to exactly that. See infra/dispatch/scm_interceptor.py + scm_broker.py.

The tool set is aggregated + de-duplicated at the gateway and ``tools/list`` is
filtered per principal by the policy engine, so one client's
``list_tools_sync()`` returns exactly the tools this agent's role may call.
"""

import os

from strands.tools.mcp import MCPClient

# Trusted per-dispatch headers the agent stamps onto the gateway client. The
# interceptor + broker read these (never LLM-supplied tool args) as server truth.
ORIGIN_HEADER = "x-dispatch-origin"  # owner/repo the dispatch came from
AGENT_HEADER = "x-dispatch-agent"  # calling agent id (workitems/docwriter/adr)


class GatewayNotConfiguredError(RuntimeError):
    """GATEWAY_MCP_URL is unset. The fleet is gateway-only — an agent cannot run
    without the gateway (it is the policy-enforcement + observability chokepoint)."""


def gateway_url() -> str:
    """The AgentCore Gateway MCP URL. REQUIRED — raises if unset (gateway-only)."""
    url = os.environ.get("GATEWAY_MCP_URL")
    if not url:
        raise GatewayNotConfiguredError(
            "GATEWAY_MCP_URL is not set — the fleet routes all MCP tool calls "
            "through the AgentCore Gateway (deploy with DeployGateway=true and set "
            "GATEWAY_MCP_URL on the agent runtime)."
        )
    return url


def build_gateway_client(
    *, dispatch_origin: str | None = None, agent: str | None = None
) -> MCPClient:
    """A single MCPClient to the AgentCore Gateway, SigV4-signed via the runtime
    role. Stamps the trusted per-dispatch headers (origin + agent) so the gateway
    interceptor/broker can enforce co-repo grouping and per-agent access. Imports
    the AWS IAM transport lazily so import doesn't require the extra dependency.

    ``dispatch_origin`` is the ``owner/repo`` the request came from (from the
    Router-set dispatch payload — trusted, resolved before the model runs);
    ``agent`` is the calling agent id. Both are omitted for non-GitHub dispatches;
    the interceptor treats an absent origin as "no cross-repo reach"."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    url = gateway_url()
    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-west-2"
    )
    headers = {}
    if dispatch_origin:
        headers[ORIGIN_HEADER] = dispatch_origin
    if agent:
        headers[AGENT_HEADER] = agent
    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=url,
            aws_region=region,
            aws_service="bedrock-agentcore",
            headers=headers or None,
        )
    )

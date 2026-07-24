"""AgentCore Gateway REQUEST interceptor — co-repo grouping enforcement.

The fleet is gateway-only: every GitHub tool call flows agent → Gateway → SCM
broker. Cedar (at the gateway) enforces per-agent tool grants + the master
eligible-repo allowlist, but Cedar's principal is the agent's runtime role —
IDENTICAL across every dispatch — so Cedar cannot express "a dispatch that
ORIGINATED in repo A may act on B and C but not D". That per-origin rule needs
the dispatch origin, which the agent stamps as a trusted request header
(``x-dispatch-origin``) when it builds the gateway client, BEFORE the model runs
(the model controls tool arguments, never transport headers — so it can't forge
or widen it).

This REQUEST interceptor runs at the gateway before the target/broker. It:
  1. Reads the trusted ``x-dispatch-origin`` + ``x-dispatch-agent`` headers.
  2. Reads the target ``owner/repo`` from the MCP tool-call arguments.
  3. Enforces ``fleet_config.coreachable_repos(origin)`` — if the call's repo
     isn't co-approved to run with the origin, it REJECTS the call with an MCP
     error (it never reaches GitHub).
  4. Injects the trusted origin + agent into the tool arguments
     (``_dispatch_origin`` / ``_dispatch_agent``) so the broker mints the token
     from SERVER TRUTH, not from anything the model could set.

Enforcement here is defense-in-line-of-fire: the broker re-checks the same
grouping and mints a repo+permission-scoped token, so even a bug here can't yield
a broad credential. See infra/dispatch/scm_broker.py.

Interceptor I/O contract (AgentCore docs): the event carries
``event.mcp.gatewayRequest`` with ``headers`` + ``body``; a REQUEST interceptor
returns ``{interceptorOutputVersion: "1.0", mcp: {transformedGatewayRequest:
{headers, body}}}`` to pass the (possibly modified) request through, or an MCP
JSON-RPC error result to short-circuit it.
"""

import json
import logging

import fleet_config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ORIGIN_HEADER = "x-dispatch-origin"
AGENT_HEADER = "x-dispatch-agent"
_TOOL_DELIM = "___"

# Injected into tool arguments as server-truth (the broker reads these, never the
# model-visible owner/repo, to decide scoping).
ORIGIN_ARG = "_dispatch_origin"
AGENT_ARG = "_dispatch_agent"

# JSON-RPC error code for an authorization failure (MCP surfaces this to the agent
# as a tool error rather than a crash).
_ERR_FORBIDDEN = -32003


def _headers(event: dict) -> dict:
    gw = (event.get("mcp") or {}).get("gatewayRequest") or {}
    # Header names are case-insensitive; normalize to lowercase for lookup.
    return {str(k).lower(): v for k, v in (gw.get("headers") or {}).items()}


def _body(event: dict) -> dict:
    gw = (event.get("mcp") or {}).get("gatewayRequest") or {}
    body = gw.get("body")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {}
    return body or {}


def _tool_and_args(body: dict) -> tuple[str, dict]:
    """Extract the tool name (prefix stripped) + arguments from a JSON-RPC
    ``tools/call`` body. Returns ("", {}) for non-tool-call requests."""
    if body.get("method") != "tools/call":
        return "", {}
    params = body.get("params") or {}
    name = params.get("name", "")
    tool = name.split(_TOOL_DELIM, 1)[1] if _TOOL_DELIM in name else name
    args = params.get("arguments")
    return tool, (args if isinstance(args, dict) else {})


def _untouched(event: dict) -> dict:
    """Forward the request unmodified — echo the original headers + body.
    transformedGatewayRequest accepts EXACTLY {headers, body}: echoing extra
    fields (path/httpMethod/context) is rejected with "Received invalid
    response from interceptor", and an empty mcp:{} makes the gateway parse an
    empty request ("Parse error - Invalid JSON format")."""
    gw = (event.get("mcp") or {}).get("gatewayRequest") or {}
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": gw.get("headers") or {},
                "body": gw.get("body") or "{}",
            }
        },
    }


def _passthrough(event: dict, body: dict) -> dict:
    """Forward the request with our modified ``body`` (injected server-truth
    args). Same exact {headers, body} shape as _untouched.

    The body's TYPE must match what the gateway delivered: the gateway sends
    ``body`` as a parsed JSON object, and returning a json.dumps'd STRING in
    its place is rejected as "Received invalid response from interceptor" —
    which surfaces to the MCP client as a 500 and kills the whole session.
    Echo a dict as a dict; only stringify if the input was a string."""
    gw = (event.get("mcp") or {}).get("gatewayRequest") or {}
    out_body = json.dumps(body) if isinstance(gw.get("body"), str) else body
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": gw.get("headers") or {},
                "body": out_body,
            }
        },
    }


def _reject(body: dict, message: str) -> dict:
    """Short-circuit the request with a TOOL ERROR result — the call never
    reaches the target/GitHub. Echoes the request id so the client can correlate.

    Shape matters twice over:
      - ``gatewayResponse`` must carry ``statusCode`` + ``headers`` + ``body``
        (like ``transformedGatewayRequest``, a partial shape is rejected by the
        gateway as "invalid response from interceptor" and surfaces as a 500 to
        the client — which Strands' MCPClient treats as a dead session, killing
        EVERY subsequent tool call in the run).
      - the payload is a JSON-RPC *result* with ``isError: true`` (an MCP tool
        error), not a JSON-RPC *error*. A protocol-level error also tears down
        the client session; a tool error is returned to the model, which can
        adapt (pick another repo, report the restriction) and keep working."""
    logger.warning("scm_interceptor: rejecting call — %s", message)
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "gatewayResponse": {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                # A parsed JSON object, not a json.dumps'd string — the gateway
                # rejects a stringified body as an invalid interceptor response
                # (same contract as transformedGatewayRequest.body).
                "body": {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Forbidden: {message}"}],
                    },
                },
            }
        },
    }


def handler(event, context=None):
    """REQUEST interceptor entry point. Enforces co-repo grouping from the trusted
    origin header, injects server-truth origin/agent, else passes through."""
    # Shape guard: if the event doesn't carry mcp.gatewayRequest where we expect
    # it, we can't parse — and we must NOT emit a transformed request with an
    # empty body (that REPLACES the real request and 500s every gateway call,
    # including initialize/tools/list). Log the actual top-level shape once so
    # the mismatch is diagnosable, and pass through untouched.
    gw = (event.get("mcp") or {}).get("gatewayRequest")
    if not isinstance(gw, dict) or "body" not in gw:
        # Can't parse → can't transform. Forward the original request rather
        # than emitting a broken transform.
        logger.error(
            "scm_interceptor: unexpected event shape (keys=%s, mcp keys=%s) — "
            "forwarding untransformed",
            sorted(event.keys()),
            sorted((event.get("mcp") or {}).keys()),
        )
        return _untouched(event)

    body = _body(event)
    tool, args = _tool_and_args(body)

    # Non-tool-call requests (initialize, tools/list, ping, …) and non-SCM tools
    # carry no repo to gate — pass them straight through untouched.
    #
    # SECURITY INVARIANT: this owner+repo gate is why every broker tool MUST
    # require both owner and repo (scm_broker._TOOLS). A tool missing either would
    # take this passthrough path, skipping co-repo enforcement AND the server-truth
    # origin/agent injection below — leaving the model-visible args in place. The
    # broker still fails closed on an absent origin, but this interceptor is the
    # primary control. See scm_broker._TOOLS; enforced by
    # test_scm_broker.test_every_tool_requires_owner_and_repo.
    if not tool or "owner" not in args or "repo" not in args:
        return _untouched(event)

    headers = _headers(event)
    origin = str(headers.get(ORIGIN_HEADER, "")).strip()
    agent = str(headers.get(AGENT_HEADER, "")).strip()
    target = f"{str(args.get('owner', '')).strip()}/{str(args.get('repo', '')).strip()}"

    # No trusted origin → no cross-repo reach is authorized. A GitHub tool call
    # must carry the dispatch origin (the agent always stamps it); its absence is
    # a misconfiguration or an attempt to bypass grouping — fail closed.
    if not origin:
        return _reject(
            body, "no dispatch origin on the request — cross-repo action refused"
        )

    reachable = set(fleet_config.coreachable_repos(origin))
    if target.strip("/").casefold() not in {r.casefold() for r in reachable}:
        return _reject(
            body,
            f"repo '{target}' is not approved to run with '{origin}' "
            f"(co-repo grouping). Onboard it into the same group, or set the "
            f"origin's co-repo mode to reach it.",
        )

    # Authorized. Inject server-truth origin + agent so the broker scopes the
    # token from these, not from the model-visible arguments.
    args[ORIGIN_ARG] = origin
    if agent:
        args[AGENT_ARG] = agent
    body.setdefault("params", {})["arguments"] = args
    return _passthrough(event, body)

"""Model construction for the fleet — Amazon Bedrock **Mantle** (OpenAI-compatible).

Every agent builds its model through ``build_model`` so the whole fleet shares
one model backend, one guardrail attachment, and one cost-attribution scheme.

The fleet runs on the **bedrock-mantle** endpoint (OpenAI-compatible), NOT the
legacy ``bedrock-runtime`` Converse/Invoke path, because Mantle gives us:
  - per-**project** cost & usage attribution (we use a project PER REPO, passed
    in the dispatch payload — the app will surface cost/usage later), and
  - flexibility to run non-Anthropic / non-Bedrock models (GPT-OSS, etc.) through
    the same client if we bring other tools in.

Auth: a short-term Bedrock **bearer token** minted from the runtime role's own
AWS credentials via ``aws-bedrock-token-generator`` (no long-lived secret to
store or rotate; the token inherits the role's ``bedrock-mantle`` permissions and
expires ≤12h). See the runtime role grant in infra/dashboard/capability_deployer.py
and the CapabilityRuntimeBoundary in infra/foundation/template.yaml.

Guardrail (fail-closed, threat-model T-1/2/3): the prompt-injection guardrail is
attached via the documented Mantle headers
(``X-Amzn-Bedrock-GuardrailIdentifier`` / ``-GuardrailVersion`` / ``-Trace``), so
it still evaluates every model call — the OpenAI-compatible surface does NOT
weaken it. If ``BEDROCK_GUARDRAIL_ID`` is unset, ``build_model`` raises, unless
``SDLC_ALLOW_MISSING_GUARDRAIL=1`` (local dev / unit tests only).

Env vars injected by each agent's AgentCore runtime (see capability_deployer):
  BEDROCK_MODEL_ID           — model id (defaults to anthropic.claude-sonnet-5)
  BEDROCK_GUARDRAIL_ID       — guardrail identifier from the foundation stack
  BEDROCK_GUARDRAIL_VERSION  — guardrail version (typically "DRAFT")
  MANTLE_ENDPOINT / AWS_REGION — resolve the bedrock-mantle base URL
Per-dispatch (NOT env — varies per request), passed by the agent from the
dispatch payload: the Mantle project id (per repo) → ``project`` arg here.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Sonnet 5 on the Mantle catalog. Sonnet 5 / Opus 4 reject an explicit
# `temperature` param (it silently degrades the call), so we never set one.
DEFAULT_MODEL_ID = "anthropic.claude-sonnet-5"
_ALLOW_MISSING_GUARDRAIL_ENV = "SDLC_ALLOW_MISSING_GUARDRAIL"


def _mantle_base_url() -> str:
    """The bedrock-mantle OpenAI-compatible endpoint for this region. Overridable
    via MANTLE_ENDPOINT for testing / non-standard partitions."""
    override = os.environ.get("MANTLE_ENDPOINT")
    if override:
        return override.rstrip("/")
    region = os.environ.get("AWS_REGION", "us-east-1")
    return f"https://bedrock-mantle.{region}.api.aws/v1"


def _bearer_token() -> str:
    """A short-term Bedrock bearer token minted from the runtime role's creds.
    No long-lived secret; the token inherits the role's bedrock-mantle perms and
    auto-expires. Falls back to AWS_BEARER_TOKEN_BEDROCK if one is pre-provided
    (e.g. local dev)."""
    pre = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if pre:
        return pre
    from aws_bedrock_token_generator import provide_token

    return provide_token(region=os.environ.get("AWS_REGION", "us-east-1"))


def _guardrail_headers() -> dict[str, str]:
    """The Mantle guardrail headers, or raise (fail-closed) when the guardrail id
    is unset — matching the historical BedrockModel posture. The OpenAI-compatible
    path attaches the guardrail via these documented headers."""
    guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID")
    guardrail_version = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    if not guardrail_id:
        if os.environ.get(_ALLOW_MISSING_GUARDRAIL_ENV) == "1":
            logger.warning(
                "BEDROCK_GUARDRAIL_ID is not set and %s=1 — building the model "
                "WITHOUT the prompt-injection guardrail. Local dev / unit tests only.",
                _ALLOW_MISSING_GUARDRAIL_ENV,
            )
            return {}
        raise RuntimeError(
            "BEDROCK_GUARDRAIL_ID is not set. Deployed runtimes must have this env "
            "var injected from the foundation stack's GuardrailId output "
            "(threat-model T-1). Set SDLC_ALLOW_MISSING_GUARDRAIL=1 only for local "
            "development or unit tests."
        )
    return {
        "X-Amzn-Bedrock-GuardrailIdentifier": guardrail_id,
        "X-Amzn-Bedrock-GuardrailVersion": guardrail_version,
        "X-Amzn-Bedrock-Trace": "ENABLED",
    }


def build_model(*, project: str | None = None, **extra):
    """Construct the fleet's Strands model on the bedrock-mantle endpoint, with the
    prompt-injection guardrail attached via headers and (optionally) a Mantle
    project for cost attribution.

    Args:
        project: the Mantle project id for cost/usage attribution — per REPO,
            supplied from the dispatch payload. When None, the call is attributed
            to the account's default project (still works, just uncredited).
        **extra: forwarded to the Strands model (e.g. ``max_tokens``, ``params``).

    Raises:
        RuntimeError: if ``BEDROCK_GUARDRAIL_ID`` is unset and the
            ``SDLC_ALLOW_MISSING_GUARDRAIL=1`` escape hatch is not set.
    """
    from strands.models.openai import OpenAIModel

    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    headers = _guardrail_headers()
    # Per-repo cost attribution: the Mantle "OpenAI-Project" header tags usage to
    # the project. Passed per-dispatch, not baked into the runtime, so one runtime
    # image serves every repo and cost still splits per repo.
    if project:
        headers["OpenAI-Project"] = project

    return OpenAIModel(
        model_id=model_id,
        client_args={
            "api_key": _bearer_token(),
            "base_url": _mantle_base_url(),
            "default_headers": headers,
        },
        **extra,
    )

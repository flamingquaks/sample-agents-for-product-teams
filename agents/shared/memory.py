"""AgentCore Memory tool provider construction, shared by every agent.

Why this exists rather than each agent calling the provider directly:
``AgentCoreMemoryToolProvider`` resolves its region as ``region or
DEFAULT_REGION`` where the library's ``DEFAULT_REGION`` is the hard-coded string
``"us-west-2"``. It never consults ``AWS_REGION``. So an agent that omits
``region=`` addresses the memory data plane in us-west-2 no matter where the
runtime (and the fleet Memory) actually live — every call comes back
``AccessDeniedException`` on ``arn:aws:bedrock-agentcore:us-west-2:...:memory/<id>``,
which reads like an IAM gap but is a wrong-region one, and the per-role grant is
correctly scoped to the ONE Memory in the stack's region. The reviewer's review
ledger silently degraded that way (every run looked like a first review). One
helper means the region can't be forgotten in a sixth agent.
"""

import os

# The Memory lives in the stack's region; the runtime gets AWS_REGION from
# AgentCore. Fall back to the same default as shared.bedrock so a local run
# without AWS_REGION behaves consistently across the fleet.
MEMORY_REGION = os.environ.get("AWS_REGION") or "us-east-1"


def memory_tools(*, memory_id: str, actor_id: str, session_id: str, namespace: str) -> list:
    """The memory tool set for one agent invocation. Callers gate on
    ``AGENTCORE_MEMORY_ID`` being set before calling.

    The provider is imported lazily (as shared.bedrock does with its model class)
    so this module — and anything importing it — stays importable in test
    environments without the agent container's strands_tools dependency."""
    from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider

    provider = AgentCoreMemoryToolProvider(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
        namespace=namespace,
        region=MEMORY_REGION,
    )
    return provider.tools

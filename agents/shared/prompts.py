"""Shared system-prompt assembly.

PM-capable agents keep two prompt templates (asana / github variants) plus a
block of backend-neutral rules whose TEXT is per-agent (workitems = agent-
trigger rules; researcher = source-citation rules — these must NOT be shared).
What IS shared is the assembly plumbing: pick the variant by backend and splice
in the shared-rules block and the project-context block.

`compose_system_prompt` assembles purely by string substitution (str.replace),
never str.format over the rule/template text. This removes a latent footgun in
the old per-agent plumbing: it `.replace`d `{shared_rules}` and then the caller
`.format`ed `{project_context}`, so a literal `{`/`}` in any shared rule (e.g. a
JSON example) would raise KeyError/IndexError at agent startup. With pure
substitution, brace characters in the content are inert.
"""

SHARED_RULES_TOKEN = "{shared_rules}"
PROJECT_CONTEXT_TOKEN = "{project_context}"


def compose_system_prompt(
    pm_backend: str,
    asana_template: str,
    github_template: str,
    shared_rules: str,
    project_context: str,
) -> str:
    """Assemble an agent's full system prompt for the selected PM backend.

    Args:
        pm_backend: "github" selects the github template; anything else (incl.
            "asana"/None) selects the asana template.
        asana_template / github_template: the agent's two prompt variants, each
            containing the {shared_rules} and {project_context} tokens.
        shared_rules: the agent's backend-neutral rules block.
        project_context: the rendered project-resources block.

    Returns:
        The fully-assembled system prompt. Substitution is done with str.replace
        so brace characters inside any input are treated literally.
    """
    backend = (pm_backend or "asana").lower()
    template = github_template if backend == "github" else asana_template
    return (
        template
        .replace(SHARED_RULES_TOKEN, shared_rules)
        .replace(PROJECT_CONTEXT_TOKEN, project_context)
    )

"""User story drafting tool.

Structured task tool — guides the agent through creating user stories
from research findings, product direction, or identified gaps.
"""

from strands import tool


@tool
def draft_user_stories(
    input_source: str,
    task_gid: str = "",
    count: str = "",
) -> str:
    """Generate user stories from research findings or product direction.

    The agent should use this tool when asked to create requirements,
    write user stories, or translate findings into actionable work items.

    Args:
        input_source: Description of what to write stories for. Can reference
            research findings, competitive gaps, or product direction.
        task_gid: Work-item id (Asana task GID or GitHub issue number) with the
            source material. Empty means use the current work-item context.
        count: Maximum number of stories to generate. Empty means generate
            as many as the input warrants.

    Returns:
        Instructions for the agent to follow when drafting user stories.
    """
    task_step = ""
    if task_gid:
        task_step = f"1. Read work item '{task_gid}' and all comments for source material.\n"
    else:
        task_step = "1. Read the originating work item and all comments for source material.\n"

    count_note = ""
    if count:
        count_note = f"\nLimit: Generate at most {count} stories.\n"

    return (
        f"Draft user stories based on: {input_source}\n"
        f"{count_note}\n"
        "Steps:\n"
        f"{task_step}"
        "2. Recall domain knowledge and user personas from memory.\n"
        "3. Identify the distinct user needs or capabilities implied by\n"
        "   the source material.\n"
        "4. For EACH story, produce:\n"
        "   - Title: 'As a [persona], I want [goal] so that [benefit]'\n"
        "   - Description: context and motivation\n"
        "   - Acceptance criteria: Given/When/Then format, testable,\n"
        "     covering happy path and error states\n"
        "   - Edge cases: at least 2-3 per story\n"
        "   - Dependencies: other tasks that must be done first\n"
        "   - RICE estimate: Reach * Impact * Confidence / Effort\n"
        "5. Check the existing backlog for overlap (search the work items in\n"
        "   your configured backend). Flag any stories that duplicate items.\n"
        "6. Create the stories as new work items in the project, following the\n"
        "   create/label/board guidance in your system prompt:\n"
        "   - Asana backend: create new Asana tasks in the project.\n"
        "   - GitHub backend: create one GitHub issue per story and add each to\n"
        "     the Projects V2 board.\n"
        "   - Tag/label each with 'researcher-generated'.\n"
        "7. Post a summary comment on the originating work item listing all\n"
        "   stories created, with links/numbers."
    )

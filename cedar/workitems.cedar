// Cedar policies specific to the Workitems agent.
// Workitems can read both systems and write to a controlled set of tools.

permit(
    principal == AgentCore::Agent::"workitems",
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    // GitHub — read
    resource.toolName == "github_list_issues" ||
    resource.toolName == "github_get_issue" ||
    resource.toolName == "github_list_prs" ||
    resource.toolName == "github_get_pr" ||
    resource.toolName == "github_list_milestones" ||
    // GitHub — write (create and comment only)
    resource.toolName == "github_create_issue" ||
    resource.toolName == "add_issue_comment" ||
    resource.toolName == "github_add_label" ||
    resource.toolName == "github_update_issue" ||
    // Asana — read
    resource.toolName == "asana_list_tasks" ||
    resource.toolName == "asana_get_task" ||
    resource.toolName == "asana_list_projects" ||
    resource.toolName == "asana_get_project_status" ||
    resource.toolName == "asana_search" ||
    resource.toolName == "asana_get_task_stories" ||
    // Asana — write (create, update, comment only)
    resource.toolName == "asana_create_task" ||
    resource.toolName == "asana_update_task" ||
    resource.toolName == "asana_create_project_status" ||
    resource.toolName == "add_comment" ||
    // GitHub Projects V2 (PM backend = github) — read board, items, fields
    resource.toolName == "projects_get" ||
    resource.toolName == "projects_list" ||
    // GitHub Projects V2 — write. projects_write covers add_project_item,
    // update_project_item, and create_project_status_update. The agent must
    // NOT call delete_project_item (removing work from the board), consistent
    // with the "never close issues / delete tasks" rule — that restriction is
    // enforced in the system prompt; the MCP server exposes delete under the
    // same projects_write tool, so cedar cannot split it at the tool level.
    resource.toolName == "projects_write" ||
    // Slack — write (post and react only, via direct Web API tools)
    resource.toolName == "slack_post_message" ||
    resource.toolName == "slack_add_reaction"
};

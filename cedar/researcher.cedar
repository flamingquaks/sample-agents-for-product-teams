// Cedar policies specific to the Researcher (business analyst) agent.
// Researcher works through Asana OR GitHub (Issues + Projects V2) depending on
// PM_BACKEND. Can read broadly, create/update work items and comments, use web
// search and code execution. No merge/close/delete of work items.

permit(
    principal == AgentCore::Agent::"researcher",
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    // Asana — read
    resource.toolName == "asana_get_task" ||
    resource.toolName == "asana_list_tasks" ||
    resource.toolName == "asana_get_task_stories" ||
    resource.toolName == "asana_get_project" ||
    resource.toolName == "asana_list_projects" ||
    resource.toolName == "asana_search" ||
    // Asana — write (create, update, comment only)
    resource.toolName == "asana_create_task" ||
    resource.toolName == "asana_update_task" ||
    resource.toolName == "add_comment" ||
    // GitHub (PM backend = github) — read issues/PRs and the board
    resource.toolName == "github_list_issues" ||
    resource.toolName == "github_get_issue" ||
    resource.toolName == "list_issues" ||
    resource.toolName == "get_issue" ||
    resource.toolName == "search_issues" ||
    resource.toolName == "projects_get" ||
    resource.toolName == "projects_list" ||
    // GitHub — write (create issues for drafted stories, comment, label; never
    // close/merge/delete). issue_write covers create/update on the GitHub MCP
    // server; projects_write covers add_project_item / update_project_item.
    resource.toolName == "github_create_issue" ||
    resource.toolName == "issue_write" ||
    resource.toolName == "add_issue_comment" ||
    resource.toolName == "github_add_label" ||
    resource.toolName == "projects_write" ||
    // Slack — write (post results only, no delete/edit)
    resource.toolName == "slack_post_message" ||
    resource.toolName == "slack_add_reaction" ||
    // Web search — competitive intelligence and market research
    resource.toolName == "web_search"
};

// Code execution — quantitative analysis (RICE, clustering, visualizations)
permit(
    principal == AgentCore::Agent::"researcher",
    action == AgentCore::Action::"ExecuteCode",
    resource
);

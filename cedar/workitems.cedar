// NOTE: When the AgentCore Gateway is deployed, the ENFORCED per-agent tool
// scope lives in infra/dashboard/fleet_policy.py (AGENT_TOOL_GRANTS + the fleet
// forbid/permit generators), rendered into gateway action shape
// (<Target>___<tool>) and synced to the policy engine. This file is the
// human-readable source-of-intent for that scope and remains advisory when the
// gateway is not deployed. Keep the two in sync.

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
    resource.toolName == "github_add_comment" ||
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
    resource.toolName == "asana_add_comment" ||
    // Slack — write (post only)
    resource.toolName == "slack_post_message" ||
    resource.toolName == "slack_post_thread"
};

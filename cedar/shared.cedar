// NOTE: When the AgentCore Gateway is deployed, the ENFORCED per-agent tool
// scope lives in infra/dashboard/fleet_policy.py (AGENT_TOOL_GRANTS + the fleet
// forbid/permit generators), rendered into gateway action shape
// (<Target>___<tool>) and synced to the policy engine. This file is the
// human-readable source-of-intent for that scope and remains advisory when the
// gateway is not deployed. Keep the two in sync.

// Shared Cedar policies for all agents.
// Forbid destructive operations across the entire fleet.

forbid(
    principal,
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    resource.toolName == "github_close_issue" ||
    resource.toolName == "github_delete_issue" ||
    resource.toolName == "github_merge_pr" ||
    resource.toolName == "github_close_pr" ||
    resource.toolName == "github_delete_branch" ||
    resource.toolName == "asana_delete_task" ||
    resource.toolName == "asana_delete_project" ||
    resource.toolName == "asana_delete_story" ||
    resource.toolName == "jira_delete_issue" ||
    resource.toolName == "jira_delete_project" ||
    resource.toolName == "confluence_delete_page" ||
    resource.toolName == "confluence_archive_page" ||
    resource.toolName == "confluence_delete_space"
};

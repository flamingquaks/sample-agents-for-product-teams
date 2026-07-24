// NOTE: When the AgentCore Gateway is deployed, the ENFORCED per-agent tool
// scope lives in infra/dashboard/fleet_policy.py (AGENT_TOOL_GRANTS + the fleet
// forbid/permit generators), rendered into gateway action shape
// (<Target>___<tool>) and synced to the policy engine. This file is the
// human-readable source-of-intent for that scope and remains advisory when the
// gateway is not deployed. Keep the two in sync.

// Cedar policies specific to the Researcher (business analyst) agent.
// Researcher works through Asana (read + create/update/comment) and has
// READ-ONLY GitHub access so backlog analysis, gap detection, and
// prioritization are grounded in the real codebase and issue tracker.
// No GitHub writes of any kind, no merge/close/delete (the credential tier
// in scm_broker.AGENT_GITHUB_PERMISSIONS is read-only too).

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
    resource.toolName == "asana_add_comment" ||
    // GitHub — read only (issues, PRs, files, history)
    resource.toolName == "github_get_file_contents" ||
    resource.toolName == "github_search_code" ||
    resource.toolName == "github_get_issue" ||
    resource.toolName == "github_list_issues" ||
    resource.toolName == "github_get_pull_request" ||
    resource.toolName == "github_list_pull_requests" ||
    resource.toolName == "github_list_commits" ||
    resource.toolName == "github_list_milestones" ||
    // Web search — competitive intelligence and market research
    resource.toolName == "web_search"
};

// Code execution — quantitative analysis (RICE, clustering, visualizations)
permit(
    principal == AgentCore::Agent::"researcher",
    action == AgentCore::Action::"ExecuteCode",
    resource
);

// NOTE: When the AgentCore Gateway is deployed, the ENFORCED Jira tool scope
// lives in infra/dashboard/fleet_policy.py (JIRA_TOOL_CLASS + AGENT_TOOL_GRANTS
// + the sdlc_allowed_projects forbid), rendered into gateway action shape
// (JiraTarget___<tool>) and synced to the policy engine. This file is the
// human-readable source-of-intent for that scope and remains advisory when the
// gateway is not deployed. Keep the two in sync.
//
// Jira capability (atlassian-connector-spec.md §B2).

// Reads are first-class for every agent (deep context) — get_issue,
// get_issue_comments, get_transitions, search_issues, list_projects,
// get_project. Writes are least-privilege per agent:
//   - workitems: add_comment, create_issue, update_issue, transition_issue,
//                assign_issue, link_issues, add_remote_link (owns ticket state)
//   - adr / docwriter: add_comment, add_remote_link (comment-only)
//   - researcher: reads only

// Container write allowlist: a Jira write tool is forbidden unless the call's
// project_key is an onboarded ("allow" mode) project on an enabled, active,
// jira-enabled site (rendered from the jira_project# rows).
forbid(
    principal,
    action in [
        AgentCore::Action::"JiraTarget___add_comment",
        AgentCore::Action::"JiraTarget___create_issue",
        AgentCore::Action::"JiraTarget___update_issue",
        AgentCore::Action::"JiraTarget___transition_issue",
        AgentCore::Action::"JiraTarget___assign_issue",
        AgentCore::Action::"JiraTarget___link_issues",
        AgentCore::Action::"JiraTarget___add_remote_link"
    ],
    resource
) unless {
    context.input has project_key &&
    context.input.project_key in onboarded_projects
};

// Destructive Jira operations are forbidden fleet-wide (structurally absent from
// the curated target schema too — this is the documented intent).
forbid(
    principal,
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    resource.toolName == "jira_delete_issue" ||
    resource.toolName == "jira_delete_project" ||
    resource.toolName == "jira_delete_comment"
};

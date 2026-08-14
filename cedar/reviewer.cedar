// NOTE: When the AgentCore Gateway is deployed, the ENFORCED per-agent tool
// scope lives in infra/dashboard/fleet_policy.py (AGENT_TOOL_GRANTS + the fleet
// forbid/permit generators), rendered into gateway action shape
// (<Target>___<tool>) and synced to the policy engine. This file is the
// human-readable source-of-intent for that scope and remains advisory when the
// gateway is not deployed. Keep the two in sync.

// Cedar policies specific to the Reviewer (PR code-review) agent.
// Reviewer reads the PR diff + surrounding code and posts diff-anchored review
// comments plus an advisory commit status. It NEVER writes code, approves,
// requests changes, merges, or gates a branch with a failing status.

permit(
    principal == AgentCore::Agent::"reviewer",
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    // GitHub — read code + the PR diff
    resource.toolName == "github_get_file" ||
    resource.toolName == "github_search_code" ||
    resource.toolName == "github_get_issue" ||
    resource.toolName == "github_list_issues" ||
    resource.toolName == "github_get_pr" ||
    resource.toolName == "github_list_prs" ||
    resource.toolName == "github_get_pr_diff" ||
    resource.toolName == "github_list_pr_files" ||
    resource.toolName == "github_list_commits" ||
    // GitHub — write (comments + review comments + advisory status only). The
    // enforced stack posts PR review comments via create_pull_request_review
    // with the review event forced to COMMENT (see scm_broker.py) — reviewer can
    // annotate a diff hunk but never approve/request-changes/merge.
    resource.toolName == "github_add_comment" ||
    resource.toolName == "github_add_pr_review_comment" ||
    resource.toolName == "github_create_commit_status" ||
    // Bedrock for review reasoning
    resource.toolName == "bedrock_invoke_model"
};

// Hard forbid: reviewer never writes code, opens PRs, or pushes.
forbid(
    principal == AgentCore::Agent::"reviewer",
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    resource.toolName == "github_create_file" ||
    resource.toolName == "github_update_file" ||
    resource.toolName == "github_push_files" ||
    resource.toolName == "github_create_branch" ||
    resource.toolName == "github_create_pr"
};

// Hard forbid: a PR review may only COMMENT — never approve or request changes
// (an approving review is an implicit sign-off). The enforced equivalent is
// fleet_policy.COMMENT_ONLY_REVIEW_TOOLS; the broker also forces event=COMMENT.
forbid(
    principal == AgentCore::Agent::"reviewer",
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    resource.toolName == "github_add_pr_review_comment" &&
    context.input has event && context.input.event != "COMMENT"
};

// Hard forbid: a commit status may only be advisory (success/pending) — never a
// failure/error that would gate a protected branch (a required failing status is
// a merge veto, adjacent to the "agents NEVER … merge PRs" rule). The broker
// clamps the state; this is the policy-layer backstop.
forbid(
    principal == AgentCore::Agent::"reviewer",
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    resource.toolName == "github_create_commit_status" &&
    context.input has state &&
    (context.input.state == "failure" || context.input.state == "error")
};

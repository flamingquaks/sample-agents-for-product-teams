// NOTE: When the AgentCore Gateway is deployed, the ENFORCED Confluence tool
// scope lives in infra/dashboard/fleet_policy.py (CONFLUENCE_TOOL_CLASS +
// AGENT_TOOL_GRANTS + the sdlc_allowed_spaces forbid), rendered into gateway
// action shape (ConfluenceTarget___<tool>) and synced to the policy engine. This
// file is the human-readable source-of-intent and remains advisory when the
// gateway is not deployed. Keep the two in sync.
//
// Confluence capability (atlassian-connector-spec.md §C2).

// Reads are first-class for every agent — get_page, get_page_children,
// get_comments, search, list_spaces, get_space. UNLIKE GitHub, Confluence reads
// are ALSO broker-scoped to the onboarded space (§C2.4): an Atlassian token is
// site-wide, so the confluence_broker enforces the space allowlist on every
// call including reads. Onboarding a space = fleet-wide READ visibility.
//
// Writes are least-privilege per agent:
//   - docwriter: create_page, update_page, add_comment, add_label (owns docs)
//   - workitems: add_comment
//   - adr: add_comment, add_label (links decisions; never edits page bodies)
//   - researcher: reads only
// Per-space write_mode (propose|direct) + write_agents are enforced in the
// broker (Cedar can't read space rows) — §C3.2.

// Container write allowlist: a Confluence write tool is forbidden unless the
// call's space_key is an onboarded ("allow" mode) space on an enabled, active,
// confluence-enabled site (rendered from the confluence_space# rows).
forbid(
    principal,
    action in [
        AgentCore::Action::"ConfluenceTarget___create_page",
        AgentCore::Action::"ConfluenceTarget___update_page",
        AgentCore::Action::"ConfluenceTarget___add_comment",
        AgentCore::Action::"ConfluenceTarget___add_label"
    ],
    resource
) unless {
    context.input has space_key &&
    context.input.space_key in onboarded_spaces
};

// Destructive / admin Confluence operations are forbidden fleet-wide
// (structurally absent from the curated target schema too).
forbid(
    principal,
    action == AgentCore::Action::"InvokeTool",
    resource
) when {
    resource.toolName == "confluence_delete_page" ||
    resource.toolName == "confluence_archive_page" ||
    resource.toolName == "confluence_move_page" ||
    resource.toolName == "confluence_delete_space"
};

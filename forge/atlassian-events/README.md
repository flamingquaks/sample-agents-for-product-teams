# atlassian-events — SDLC Agent Fleet Forge forwarder

One Forge app that forwards Jira + Confluence events to the fleet's per-product
webhook endpoints, so `@sdlc-agents <agent>` mentions on issues and page/inline
comments dispatch through the router, and label/transition/create events drive
the automation engine. See `docs/specs/atlassian-connector-spec.md` §A5.

**Why Forge (not admin-registered webhooks):** Confluence Cloud has no
admin-registered webhook UI, and Jira System WebHooks need copy-pasted URLs +
secrets that silently rot. Forge product triggers are declarative manifest state
that never expires, and every delivery carries a **Forge Invocation Token** the
receiver verifies against Atlassian's JWKS — **no shared secret exists** to
capture, store, or rotate.

## Deploy (fleet operators, once)

```
python scripts/deploy_forge_atlassian.py --stage dev --region us-east-1 \
    --site-id <atlassian-cloud-id>
```

The script resolves the fleet webhook base from the CloudFormation stack outputs
(`JiraWebhookEndpoint`/`ConfluenceWebhookEndpoint`), sets the Forge environment
variables (`FLEET_WEBHOOK_BASE`, `FLEET_SITE_ID`), runs `forge deploy`, and prints
the private installation link.

## Install (per site, admin)

From the dashboard **Connectors → Atlassian → app-install card**: open the
private installation link (no development-mode toggle needed) → the manifest's
scopes + egress are shown up front → install → **Verify delivery** turns green
once the receiver stamps `webhook_last_seen`.

One install covers **both** products; events for a product with no onboarded/
enabled site row are dropped at the receiver. Uninstalling silences both products
on that site — surfaced by the per-product liveness going stale on the connector
page.

/**
 * atlassian-events — the shared Forge event forwarder (atlassian-connector spec
 * §A5). One thin forwarder per product: each product trigger invokes its handler,
 * which forwards the raw event payload to the per-product fleet webhook endpoint
 * with a Forge Invocation Token (FIT) the receiver verifies (RS256 vs Atlassian's
 * JWKS). No shared secret exists.
 *
 * Transport only: the app performs NO Atlassian REST reads/writes — the fleet's
 * service-account token does that (§A6). The site id is baked into the
 * installation environment at deploy time AND cross-checked against the payload's
 * cloudId at the receiver (belt-and-braces).
 */

import { fetch } from "@forge/api";

const WEBHOOK_BASE = process.env.FLEET_WEBHOOK_BASE; // e.g. https://api.../dev
// Optional pin: when set, ONLY this site's events are forwarded. Normally unset —
// the site id is derived per invocation from the Forge context's cloudId, so ONE
// deploy serves every installed site (install-anywhere; the receiver still
// cross-checks cloudId against its site row and drops unknown sites).
const SITE_ID_PIN = process.env.FLEET_SITE_ID || "";

/** The installation's cloud id, from the invocation context (present on product
 * trigger invocations; several context shapes tolerated across runtime
 * versions), the event payload, or the deploy-time pin. */
function resolveSiteId(event, context) {
  const fromContext =
    (context && (context.cloudId || (context.installContext || "").split("/").pop())) || "";
  const fromEvent = (event && (event.cloudId || (event.context || {}).cloudId)) || "";
  return SITE_ID_PIN || fromContext || fromEvent || "";
}

/**
 * Forge automatically attaches a Forge Invocation Token to backend fetch calls
 * to a declared `external.fetch.backend` egress when `authorization: forge` is
 * requested — the receiver reads it from the `x-forge-invocation-token` header
 * (or Authorization: Bearer). We also send it explicitly from the request
 * context when present, so the contract is robust across Forge runtime versions.
 */
/** The Forge product-trigger event type (e.g. "avi:jira:updated:issue"). Forge
 * exposes it on the invocation context and/or the event; we surface it on the
 * forwarded body as a STABLE `eventType` field so the receiver + automation
 * fact-normalizers key off one reliable value instead of guessing at the
 * payload's native shape (which differs between classic webhooks and product
 * triggers). Harmless when already present. */
function eventType(event, context) {
  return (
    (event && (event.eventType || event.type || event.webhookEvent)) ||
    (context && context.eventType) ||
    ""
  );
}

async function forward(product, event, context) {
  const siteId = resolveSiteId(event, context);
  if (!WEBHOOK_BASE || !siteId) {
    console.error(
      `atlassian-events: cannot forward (webhookBase=${!!WEBHOOK_BASE} siteId=${!!siteId})`,
    );
    return;
  }
  if (SITE_ID_PIN && siteId !== SITE_ID_PIN) {
    console.warn("atlassian-events: event cloudId does not match the pinned site — dropped");
    return;
  }
  const url = `${WEBHOOK_BASE}/${product}/webhook/${siteId}`;
  const token = (context && context.invocationToken) || "";
  // Forward the event verbatim plus a stable eventType envelope field (only
  // added when the payload doesn't already carry one).
  const et = eventType(event, context);
  const body = et && !(event && event.eventType) ? { ...event, eventType: et } : event;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "x-forge-invocation-token": token } : {}),
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      // Non-2xx (e.g. 503 while the fleet JWKS is briefly unreachable) — throw so
      // Forge retries the trigger delivery rather than dropping the event.
      console.warn(`atlassian-events: ${product} forward -> HTTP ${resp.status}`);
      if (resp.status >= 500) {
        throw new Error(`fleet endpoint ${resp.status}`);
      }
    }
  } catch (err) {
    console.error(`atlassian-events: ${product} forward failed`, err);
    throw err; // let Forge retry
  }
}

export async function forwardJira(event, context) {
  await forward("jira", event, context);
}

export async function forwardConfluence(event, context) {
  await forward("confluence", event, context);
}

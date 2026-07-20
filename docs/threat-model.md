# Threat Model — PDLC Agent Fleet

**Version:** 1.8
**Date:** 2026-07-20
**Status:** Living document. Describes the fleet as it currently ships.
**Methodology:** Aligned with the [AWS Threat Designer](https://aws.amazon.com/blogs/machine-learning/accelerate-threat-modeling-with-generative-ai/) approach — identify assets, map data flows, enumerate threats (MITRE ATT&CK / OWASP), and document how each threat is mitigated today or why it is accepted.

---

## 1. System Overview

The PDLC Agent Fleet is a multi-agent system on Amazon Bedrock AgentCore Runtime. Users trigger agents via `@mention` in GitHub or Asana. HMAC-verified **webhook** Lambdas (GitHub App + Asana) async-invoke a Dispatch Router Lambda, which resolves the mention, checks authorization, applies a prompt-injection guardrail, and invokes the appropriate agent container. Agents interact with external platforms (GitHub, Asana) exclusively through the AgentCore Gateway (gateway-only); model inference runs on the Bedrock Mantle endpoint. Fleet configuration (agents/capabilities and onboarded repos) is UI-driven from an operator dashboard whose API is authorized by Amazon Verified Permissions.

### 1.1 Component Inventory

| ID | Component | Type | Description |
|----|-----------|------|-------------|
| C-1 | GitHub App Webhook Lambda (`github_webhook.py`) | AWS Lambda + API Gateway | Public HTTPS endpoint; verifies the App's HMAC `X-Hub-Signature-256`; extracts `@mention` from `issue_comment`/`pull_request_review_comment`; async-invokes the Dispatch Router. **Replaces the retired `agent-dispatch.yml` GitHub Actions workflow** — one App webhook serves every onboarded repo, so no per-repo workflow or repo-side AWS credential is needed |
| C-2 | Asana Webhook Lambda | AWS Lambda + API Gateway | Public HTTPS endpoint; verifies HMAC signature; forwards events to Dispatch Router |
| C-3 | Dispatch Router Lambda | AWS Lambda | Resolves agent, edge guardrail check, checks auth/concurrency, records assignment, invokes AgentCore Runtime |
| C-4 | AgentCore Runtimes (×4) | Bedrock AgentCore | Containerized agents (workitems, researcher, docwriter, adr) running Strands SDK; model = Claude Sonnet 5 via Bedrock Mantle (C-15) |
| C-5 | DynamoDB (`dispatch-assignments`) | Database | Assignment state tracking with TTL-based expiry |
| C-6 | SSM Parameter Store | Secrets/Config | Agent registry (String), OAuth tokens, GitHub App id/slug + webhook secret (SecureString/String); App private key is in Secrets Manager |
| C-7 | S3 Artifacts Bucket | Object Storage | Agent artifacts, screenshots, test results |
| C-8 | Cedar Policies (agent tool access) | Policy Files + Gateway engine | Per-agent allow/deny rules for tool invocations; the enforced form runs in the AgentCore Gateway policy engine (`fleet_policy.py`), `cedar/*.cedar` is the advisory source |
| C-9 | GitHub SCM broker / Gateway target | AWS Lambda (gateway target) | The `scm-broker-${STAGE}` Lambda mints per-owner GitHub App installation tokens and calls the GitHub REST API; the gateway holds no GitHub credential |
| C-10 | Asana MCP Server | External API | `mcp.asana.com/v2/mcp` — agents read/write Asana via OAuth, fronted by the gateway `AsanaTarget` |
| C-11 | *(retired)* GitHub OIDC Provider | IAM Federation | **Removed.** The old GitHub Actions deploy/dispatch path (OIDC provider + CI deploy role) has been retired — there is no CI deploy path. Kept as a stable ID; superseded by C-1 (webhook triggers) and the UI-driven onboarding pipeline (C-16, C-17) |
| C-12 | ECR Repositories | Container Registry | One per agent; `IMMUTABLE` tag policy; images built by the shared `sdlc-agent-builder-${STAGE}` CodeBuild project (on onboard + the weekly security rebuild), scanned on push |
| C-13 | Amazon Verified Permissions policy store (`DashboardPolicyStore`) | AVP / Cedar | Authorizes the **dashboard API** (distinct from C-8). `auth.is_operator`/`is_admin` call AVP `IsAuthorized` with `Read` (operators+admins) / `Write` (admins) actions; 3 static Cedar policies in the foundation template; fail-closed |
| C-14 | AgentCore Gateway + Cedar engine + REQUEST interceptor | Bedrock AgentCore | The gateway-only tool-call chokepoint. Cedar engine (default-deny, forbid-wins) enforces per-agent tool grants + repo allowlist; the `scm-interceptor` enforces per-origin co-repo grouping from the trusted `x-dispatch-origin` header |
| C-15 | Bedrock Mantle endpoint + shared fleet project | Managed model API | OpenAI-compatible `bedrock-mantle` endpoint serving `anthropic.claude-sonnet-5`. Auth is a short-term bearer token minted from the runtime role (no stored secret). A single fleet-wide Mantle **project** (`MantleProjectId` stack param → `MANTLE_PROJECT_ID` runtime env) is set as the `OpenAI-Project` header for cost attribution — one project fleet-wide because a dispatch may span repos |
| C-16 | Shared CodeBuild build project (`sdlc-agent-builder-${STAGE}`) | CodeBuild | The single agent-agnostic build project, parameterized by `AGENT_NAME`; builds `agents/<name>` and pushes to ECR (C-12). Started by the admin API (which holds only `codebuild:StartBuild`) on onboard and by the weekly rebuild schedule |
| C-17 | Capability-deployer Lambda (`capability-deployer-${STAGE}`) + `CapabilityRuntimeBoundary` | AWS Lambda + IAM managed policy | Invoked only by the CodeBuild-completion EventBridge event. **The only component holding `iam:CreateRole`/`PassRole` + `create/update-agent-runtime`** — creates each per-agent runtime role under IAM path `/sdlc-agents/capabilities/*`, capped by the `CapabilityRuntimeBoundary` permissions boundary, then deploys the AgentCore runtime, waits READY, and republishes the registry |

---

## 2. Data Flow Diagram

The current architecture diagram is maintained as Mermaid at
[`docs/assets/architecture.mmd`](assets/architecture.mmd) (rendered/explained in
[`docs/aws-deploy.md` §0](aws-deploy.md#0-architecture)). The trigger path is now
HMAC-verified **webhooks** (no GitHub Actions / OIDC), tool calls are **gateway-only**,
and model calls go to the **Bedrock Mantle** endpoint. Text summary:

```
   External platforms (GitHub, Asana)
            │  @mention events
            ▼
   ┌───────────────────────────────────────────────┐  TRUST BOUNDARY: webhook edge
   │ API Gateway (public HTTPS)                      │
   │  GitHub App webhook Lambda  (HMAC X-Hub-Sig-256)│
   │  Asana webhook Lambda       (HMAC signature)    │
   └───────────────┬─────────────────────────────────┘
                   │ async Lambda invoke (verified events only)
                   ▼
   ┌───────────────────────────────────────────────┐  TRUST BOUNDARY: AWS Account
   │ Dispatch Router Lambda                          │
   │  • edge apply_guardrail (bedrock-runtime)       │
   │  • resolve agent + authorization allowlist      │
   │  • registry from SSM (rendered from fleet-config)│
   │  • concurrency check + assignment (DynamoDB)     │
   └───────────────┬─────────────────────────────────┘
                   │ InvokeAgentRuntime (instruction + source_context)
                   ▼
   ┌───────────────────────────────────────────────┐  AgentCore Runtime (per-agent isolation)
   │ workitems · researcher · docwriter · adr        │
   │   model calls → Bedrock Mantle (bearer token,   │
   │     guardrail headers, OpenAI-Project=fleet)     │
   │   all tool calls → AgentCore Gateway (SigV4)     │
   └───────────────┬─────────────────────────────────┘
                   │ gateway-only (Cedar engine + REQUEST interceptor)
                   ▼
   ┌───────────────────────────────────────────────┐  TRUST BOUNDARY: External APIs
   │ SCM broker (per-owner GitHub App token) → GitHub │
   │ AsanaTarget → Asana MCP                          │
   └─────────────────────────────────────────────────┘

   Control plane (separate): Dashboard SPA → API Gateway (Cognito) → query/admin
   Lambdas, each API request authorized by Amazon Verified Permissions (Cedar).
   Onboarding: admin (codebuild:StartBuild) → CodeBuild → ECR → build-completion
   EventBridge → capability-deployer (privileged IAM) → AgentCore runtime + registry.
```

### 2.1 Data Flows

| ID | From → To | Data | Protocol | Auth |
|----|-----------|------|----------|------|
| DF-1 | GitHub → GitHub App Webhook Lambda | Comment body, user login, issue metadata (delivery payload) | HTTPS (GitHub App webhook) | HMAC-SHA256 `X-Hub-Signature-256` verified against the App webhook secret |
| DF-2 | GitHub App Webhook Lambda → Dispatch Router | Full comment + server-fetched issue context as JSON payload | AWS Lambda async invoke | IAM execution role |
| DF-3 | Asana → API Gateway | Webhook event payload (story/task changes) | HTTPS POST | HMAC-SHA256 signature |
| DF-4 | Asana Webhook Lambda → Asana API | Task/story fetch requests | HTTPS | Bearer PAT from SSM |
| DF-5 | Asana Webhook Lambda → Dispatch Router | Normalized event payload | Lambda async invoke | IAM execution role |
| DF-6 | Dispatch Router → SSM | Registry fetch (rendered from fleet-config capability rows) | AWS API | IAM execution role |
| DF-7 | Dispatch Router → DynamoDB | Assignment create/query | AWS API | IAM execution role |
| DF-8 | Dispatch Router → AgentCore Runtime | Instruction + context as JSON | `InvokeAgentRuntime` | IAM execution role (scoped to runtime/runtime-endpoint ARNs in this account+region) |
| DF-9 | Agent → SSM / Secrets Manager | Credential fetch (Asana OAuth tokens); GitHub App key is read only by the broker/reply Lambdas | AWS API | AgentCore runtime role |
| DF-10 | Agent → Gateway → SCM broker → GitHub | Issue/PR reads, comment/code writes | HTTPS (SigV4 to gateway) | Per-owner GitHub App installation token, minted server-side, scoped per co-repo group + per-agent tier |
| DF-11 | Agent → Gateway → Asana MCP | Task reads, comment writes | HTTPS (SigV4 to gateway) | OAuth2 access token |
| DF-12 | Agent → Bedrock Mantle | LLM inference (Claude Sonnet 5), guardrail applied via Mantle headers | HTTPS (OpenAI-compatible) | Short-term Bedrock bearer token minted from the runtime role (`aws-bedrock-token-generator`); `OpenAI-Project` = the fleet's shared Mantle project (`MANTLE_PROJECT_ID`) |
| DF-12b | Dispatch Router / ADR agent → bedrock-runtime | Edge `apply_guardrail` (Router); Titan embeddings (ADR) | AWS API | IAM/runtime role (classic `bedrock-runtime`) |
| DF-13 | CodeBuild (`sdlc-agent-builder-${STAGE}`) → ECR | Container image push (per-build tag, immutable) | HTTPS | CodeBuild service role |
| DF-14 | Dashboard SPA → API Gateway → query/admin Lambda | Run history reads (operators); capability/repo config writes (admins) | HTTPS | Cognito JWT at the API Gateway authorizer; every request authorized by AVP (`IsAuthorized`, Read/Write) |
| DF-15 | Admin Lambda → CodeBuild | Start agent build (`codebuild:StartBuild`) on capability onboard/edit | AWS API | Admin Lambda role (no privileged IAM — holds only StartBuild + registry publish) |
| DF-16 | build-completion EventBridge → capability-deployer → IAM/AgentCore | Create per-agent runtime role (boundary-capped) + create/update runtime + republish registry | AWS API | Capability-deployer role (`CreateRole`/`PassRole` scoped to `/sdlc-agents/capabilities/*` with the mandatory permissions boundary) |

---

## 3. Threat Catalog

Threats are categorized using [STRIDE](https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats) and mapped to [MITRE ATT&CK](https://attack.mitre.org/) and [OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/) where applicable. Each entry documents the current control posture: **Mitigated**, **Partially mitigated**, **Accepted** (acknowledged as a known limitation of this reference architecture), or **Open** (not yet addressed).

### 3.1 Prompt Injection (LLM-Specific)

| ID | Threat | Severity | Component | OWASP LLM | Status |
|----|--------|----------|-----------|-----------|--------|
| T-1 | **Indirect prompt injection via issue/task content** | **Critical** | C-4 | LLM01 | Partially mitigated |
| T-2 | **Direct prompt injection via @mention** | **High** | C-3, C-4 | LLM01 | Partially mitigated |
| T-3 | **Cross-agent prompt injection** | **High** | C-4 | LLM01 | Partially mitigated |

An attacker can craft a GitHub issue body, Asana task description, or the comment containing the `@mention` itself to insert instructions the agent reads as context. Injected instructions can override the system prompt; chained injection across agents (one agent creates a task that manipulates another) amplifies blast radius.

**Current controls (defense-in-depth, ordered edge → runtime → structure):**

1. **Edge filter at the Dispatch Router.** The body of every inbound `@mention` is scored by Amazon Bedrock Guardrails (`PROMPT_ATTACK` filter, `InputStrength: MEDIUM`) before the agent is invoked. A trip records the assignment as `blocked_guardrail`, posts a block-notice reply to the originating thread (no silent failures), emits a `GuardrailTripped` CloudWatch metric, and returns 400. This is the primary defense against T-2 and the first line against T-3. Attackers who probe the filter receive the same block-notice legitimate users do — there is no differentiated error path. `MEDIUM` is the shipping strength after `HIGH` was found to block benign user messages at high false-positive rate against the Dispatch Context wrapper; operators running against more hostile inputs can raise it in `infra/foundation/template.yaml`.

2. **Runtime guardrail on every agent's model invocation.** The same guardrail is attached to each agent's model call — the fleet runs on the OpenAI-compatible **Bedrock Mantle** endpoint, and the guardrail is applied via the documented Mantle headers (`X-Amzn-Bedrock-GuardrailIdentifier` / `-GuardrailVersion` / `-Trace`), so the OpenAI-compatible surface does not weaken it. `build_model` (`agents/shared/bedrock.py`) **fails closed**: it raises if `BEDROCK_GUARDRAIL_ID` is unset (except in explicitly flagged local dev/tests), so an agent cannot come up with an unguarded model path. Content the agent fetches from external platforms after dispatch (task notes, issue bodies, PR descriptions pulled via the gateway) is scored server-side on the way into the model. This is the only layer that sees T-1 — an edge-only check cannot, because the attack arrives via a trusted-looking tool response, not via the mention comment. `OutputStrength` on this guardrail is `NONE` by design: agent outputs are bounded by structural controls (approval pattern, no destructive tools, per-agent IAM, and the Gateway Cedar engine) rather than content filtering.

3. **Model-level resistance.** Claude Sonnet 5 (the fleet's default model via Mantle) has built-in resistance to adversarial prompts. Treated as a baseline, not a boundary — probabilistic like any LLM defense.

4. **Structural controls on what a subverted agent can do.** Agents have no destructive tools (no close-issue, merge-PR, or delete-task primitives). Approval-pattern workflows require a human to accept proposed work before it lands. Per-agent IAM runtime roles grant only the specific SSM parameters and MCP endpoints each agent needs. A prompt-injected agent can still misuse a legitimate tool (e.g. post a misleading comment), but cannot escalate into actions the architecture doesn't expose.

5. **Authorization at the Router (T-4).** The per-agent `authorization.users` allowlist rejects any sender not explicitly permitted to invoke that agent. Cross-agent chains only propagate between agents the operator has paired, which bounds the blast radius of T-3 to the topology of the allowlists.

**Residual risk.** Bedrock Guardrails is probabilistic, not deterministic — a sufficiently novel prompt-attack pattern can slip past either evaluation point. Controls 3–5 bound what *happens* when it does: a subverted agent is restricted to the tools its IAM role and MCP servers allow, cannot invoke peers outside its allowlist, and cannot perform destructive actions. None of this prevents exfiltration via legitimate write channels (T-15) — Cedar runtime enforcement (T-5) is the roadmap item that would close that gap by intercepting individual tool calls. Operators deploying against untrusted input (public repos, external collaborators) should layer a classifier-based pre-filter in the Dispatch Router on top of Guardrails and revisit the Accepted findings before go-live.

**Detection.** `GuardrailTripped` fires on every block — the rate is the signal. A sustained rise above ~10 trips/hour triggers an alarm and warrants operator attention (either an active probing campaign or a false-positive spike worth re-tuning filter strength for). Guardrail-service outages fail closed and emit `GuardrailError`; any non-zero rate pages. All trip decisions are written to the assignments table with a 30-day TTL, providing an audit trail independent of CloudWatch log retention.

### 3.2 Authorization & Access Control

| ID | Threat | Severity | Component | STRIDE | Status |
|----|--------|----------|-----------|--------|--------|
| T-4 | **Agent registry authorization defaults** | — | C-3 | Elevation of Privilege | Mitigated |
| T-5 | **Cedar policies not enforced at runtime** | **High** | C-8 | Tampering | Partially mitigated |
| T-6 | **Dispatch Router IAM scope** | — | C-3 | — | Mitigated |
| T-7 | **GitHub OIDC trust scope** | — | C-11 | — | **Superseded** (OIDC/CI deploy path retired) |
| T-29 | **Dashboard API authorization (AVP)** | — | C-13 | Elevation of Privilege | Mitigated |

**T-4 (Mitigated):** A capability's `authorization.users` defaults to `[]` and the Dispatch Router fails closed — an empty allowlist returns 403 with a log line instructing the operator to populate the capability's user list in the dashboard Admin view (which re-renders the SSM registry). The wildcard `"*"` is still accepted for operators who explicitly opt into an open-by-default posture, but it is no longer the shipping default. Cross-agent invocation is permitted by listing a peer agent's bot identity (GitHub login or Asana user GID) in the callee's `users` list — see T-23 for the design intent and runaway-chain defense.

**T-5 (Partially mitigated):** Cedar policy files under `cedar/*.cedar` express per-agent allow/deny rules for tool calls; the **enforced** form lives in `infra/dashboard/fleet_policy.py` and is evaluated by the **AgentCore Gateway policy engine** in the invocation path (the fleet is gateway-only, so every tool call passes through it). The engine is default-deny + forbid-wins: destructive tools are unconditionally forbidden, per-agent permits grant only each agent's `AGENT_TOOL_GRANTS`, and a repo-allowlist forbid blocks tools targeting non-onboarded repos. Residual: the engine is rolled out `LOG_ONLY` first — until an operator flips `GatewayPolicyEnforcement=ACTIVE`, Cedar *tool-grant* deny decisions log rather than block (the co-repo interceptor and the per-call scoped credential still enforce regardless — see T-11). The `cedar/*.cedar` files themselves remain advisory; `fleet_policy.py` is authoritative.

**T-6 (Mitigated):** The Dispatch Router Lambda's IAM policy grants only `bedrock-agentcore:InvokeAgentRuntime`, scoped to `arn:aws:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:runtime/*` and the corresponding `runtime/*/runtime-endpoint/*` shape. The router cannot invoke runtimes in other accounts or regions, and cannot call `bedrock:InvokeAgent` on the legacy Bedrock Agents service.

**T-7 (Superseded):** This threat covered the GitHub Actions OIDC deploy role's trust scope. That path is **retired** — there is no GitHub OIDC provider, no CI deploy role, and no `agent-dispatch.yml`/`claude-code.yml` workflow in the shipping fleet. Triggers now arrive via HMAC-verified webhooks (T-30) and all build/deploy is server-side (CodeBuild + capability-deployer, T-31). The ID is kept stable for history; the OIDC trust-scope guidance no longer applies to this architecture.

**T-29 (Mitigated):** The dashboard exposes fleet-wide, cross-user activity data and the fleet's *configuration surface* (onboard/offboard agents and repos), so its API needs authorization beyond a valid login. Authorization is decided by **Amazon Verified Permissions** evaluating Cedar policies (`infra/dashboard/auth.py` → `IsAuthorized`), decoupled from app code: two coarse actions today — `Read` (GET routes, gated by `is_operator`) and `Write` (POST/PUT/DELETE, gated by `is_admin`) — expressed as three static Cedar policies in the foundation template (operators→Read, admins→Read, admins→Write). The API Gateway Cognito authorizer validates the JWT and forwards its claims; `auth.py` builds the principal + `cognito:groups` parent entities and calls AVP. **Fail-closed at every step:** no authenticated subject → deny; any AVP error or outage → deny; any non-`ALLOW` decision → deny. When `AVP_POLICY_STORE_ID` is unset (AVP not deployed / unit tests) the same policy is evaluated locally with identical semantics, also fail-closed. This is **distinct from the agent-tool Cedar engine at the Gateway (C-14)** — that authorizes what an *agent* may do to GitHub/Asana; this authorizes what a *human operator* may do to the fleet config. Adding a permission is adding a Cedar policy, not a code change. **Residual (availability, not confidentiality):** because it is fail-closed, an AVP outage denies all dashboard API calls until service is restored — the API becomes unavailable but never leaks or accepts an unauthorized write. Acceptable: the dashboard is an operator console, not on the agent hot path (dispatch/agent execution do not depend on AVP).

### 3.3 Credential & Secret Management

| ID | Threat | Severity | Component | STRIDE | Status |
|----|--------|----------|-----------|--------|--------|
| T-8 | **Asana PAT lifetime in Lambda execution environment** | — | C-2 | Information Disclosure | Mitigated |
| T-9 | **Webhook secret self-write during handshake** | — | C-2 | Tampering | Mitigated |
| T-10 | **OAuth token refresh failure leaves stale credentials** | **Low** | C-4, C-6 | Denial of Service | Accepted |
| T-11 | **GitHub credential scope may be overly broad** | **Medium** | C-9 | Elevation of Privilege | Mitigated |

**T-8 (Mitigated):** `infra/dispatch/asana_webhook.py` fetches the Asana PAT on demand inside `asana_get`, caches it only on an `invocation_state` dict that goes out of scope when the handler returns, and never retains it on a module-level global. The webhook secret is likewise fetched per invocation. A memory-disclosure or verbose-log incident exposes at most the secrets used by the single request that was in flight, not the secrets used by every prior request in the same execution environment.

**T-9 (Mitigated):** The asana-webhook Lambda's IAM policy grants only `ssm:GetParameter` on the Asana PAT and webhook-secret parameters in steady state — it cannot overwrite the secret. The handshake still works because registration is gated through `scripts/bootstrap_asana_webhook.py`: the operator runs the script, which attaches a temporary inline `ssm:PutParameter` policy (scoped to the single parameter) to the Lambda's execution role, calls the Asana webhooks API, polls SSM until the handshake writes the secret, and removes the inline policy. Outside that registration window, an attacker who can replay Asana's handshake receives a 403 — the Lambda logs "handshake PutParameter denied" and refuses to overwrite the stored secret.

**T-10 (Accepted):** Agents surface OAuth refresh errors in logs but there is no automated rotation or CloudWatch alarm. Operators are expected to notice failed runs and re-run `pdlc-agents-connect-asana`. Acceptable for a reference architecture; production deployments should add alarms on SSM parameter age.

**T-11 (Mitigated):** The GitHub credential is now a **per-owner GitHub App installation token** at every call site — the shared PAT and its `GITHUB_AUTH_MODE` selector have been retired. An installation token is bounded by construction: it can only touch repos in that owner's installation, with the App's fine-grained permission set (Contents/Issues/PRs R&W, Metadata Read). Onboarding verifies, before activating a repo, that the App is installed on the repo's owner and can reach the repo, resolving a per-owner `installation_id` so one App bounds many individual + org owners (`infra/dashboard/github_client.py`). All three call sites mint just-in-time, scoped to the dispatched repo's owner: the **agent runtime** (`agents/shared/tools/github_app.py`), the **dispatch reply Lambda** (`infra/dispatch/github_app.py`), and — in gateway mode — the **SCM broker Lambda target** (`infra/dispatch/scm_broker.py`), which mints the token server-side per call so the gateway itself holds no GitHub credential. This closes the "one over-broad credential" gap at the credential layer. Defense-in-depth still complements it at the tool-call layer: agent tool calls route through the **AgentCore Gateway** whose **Cedar policy engine** enforces the admin repo allowlist (a `forbid` on GitHub write tools whose target repo isn't enabled + multi-repo-eligible) plus per-agent `permit` policies keyed on each runtime role's ARN (`infra/dashboard/fleet_policy.py`), and the Dispatch Router rejects mentions from non-onboarded repos (`infra/dispatch/fleet_config.py`). The fleet is **gateway-only** and the credential is narrowed per dispatch. Every GitHub tool call flows agent → AgentCore Gateway → REQUEST interceptor (`infra/dispatch/scm_interceptor.py`) → SCM broker (`infra/dispatch/scm_broker.py`); agents hold no GitHub credential. The agent stamps the dispatch origin + its agent id as trusted request headers (`x-dispatch-origin` / `x-dispatch-agent`) when it builds the gateway client — runtime code that runs BEFORE the model, so a prompt-injected agent can't forge or widen them (the model controls tool arguments, never transport headers). The interceptor enforces per-origin co-repo grouping (`config_store.coreachable_repos` — isolated/group/all, spanning owners) and rejects an out-of-group call before it reaches GitHub; the broker re-checks grouping (defense in depth) and mints a token scoped (via GitHub's installation-token `repositories` param) to the called repo and (via `permissions`) to the calling agent's tier ∩ the tool's least privilege (`fleet_policy.AGENT_GITHUB_PERMISSIONS` — e.g. workitems gets no `contents`, so it cannot push code; adr gets `contents:read` only). So a dispatch from repo A can neither act on a repo it isn't approved to run with, nor exceed its agent's product access — enforced at the header/interceptor layer AND the credential layer. Residual (operational, not credential-scope): the gateway policy engine is rolled out `LOG_ONLY` first — until it is `ACTIVE`, Cedar's *tool-grant* deny decisions log rather than block (the co-repo interceptor + the scoped credential still enforce regardless), and the policy tool names must be reconciled against the live gateway manifest (`scripts/check_gateway_manifest.py`).

### 3.4 Network & API Security

| ID | Threat | Severity | Component | STRIDE | Status |
|----|--------|----------|-----------|--------|--------|
| T-12 | **Public API Gateway endpoint for Asana webhooks** | **Medium** | C-2 | Spoofing, DoS | Partially mitigated |
| T-13 | **No API Gateway throttling or WAF configured** | **Medium** | C-2, C-1 | Denial of Service | Open |
| T-14 | **Dispatch Router 900-second timeout** | **Low** | C-3 | Denial of Service | Accepted |
| T-30 | **Public GitHub App webhook endpoint** | **Medium** | C-1 | Spoofing, DoS | Partially mitigated |

**T-12 (Partially mitigated):** The `/asana/webhook` endpoint is public but every request's HMAC-SHA256 signature is verified against the stored webhook secret before any downstream work is done. Forged events are rejected at ingress. What remains open: volumetric DDoS and replay — see T-13.

**T-13 (Open):** The SAM template sets no throttling, burst, or WAF configuration on the webhook API — this applies to **both** public webhook endpoints (Asana C-2 and the GitHub App C-1). A flood of malformed requests still triggers Lambda cold starts and signature-verification work. Fix path: add API Gateway usage-plan throttling and optionally attach AWS WAF for IP-based rate limiting.

**T-30 (Partially mitigated):** The GitHub App webhook path replaces the retired `agent-dispatch.yml` (the old trigger relied on GitHub Actions + an OIDC deploy role in every repo — see the C-11/T-7 retirement). The `/github/webhook` endpoint is public, but `github_webhook.py` authenticates **every** delivery: it computes an HMAC-SHA256 over the exact raw body and compares it (constant-time, `hmac.compare_digest`) against `X-Hub-Signature-256` using the App's webhook secret. A missing/unset secret **fails closed** (503, refuse events); a bad signature returns 401 before any downstream work. The secret is an SSM SecureString fetched per invocation and held only in local scope, never a module global (mirrors T-8). This webhook signature check is the direct replacement for the OIDC trust boundary that used to gate the GitHub → AWS hop: instead of federating a CI role, the fleet verifies that each event genuinely came from its own GitHub App. Server-side issue/PR context enrichment uses a per-repo, least-privilege GitHub App installation token (not a broad Actions token). What remains open (shared with T-13): volumetric DDoS and replay have no throttling/WAF yet.

**T-14 (Accepted):** The Dispatch Router Lambda timeout is 900 seconds to accommodate the synchronous `InvokeAgentRuntime` call for long agent runs. A hung agent ties up the Lambda execution environment; combined with concurrency limits, this could delay legitimate dispatches. Acceptable for the scale this architecture targets.

### 3.5 Data Integrity & Exfiltration

| ID | Threat | Severity | Component | STRIDE | Status |
|----|--------|----------|-----------|--------|--------|
| T-15 | **Agent exfiltrates data to unauthorized destinations** | **High** | C-4 | Information Disclosure | Accepted |
| T-16 | **DynamoDB assignment records contain full instruction text** | **Low** | C-5 | Information Disclosure | Mitigated |
| T-17 | **Agent output posted to wrong platform context** | **Medium** | C-4 | Tampering | Open |

**T-15 (Accepted):** A prompt-injected agent could use a write-capable tool (GitHub comment, Asana task, Researcher's Tavily web search) to exfiltrate sensitive context. The primary defense would be Cedar runtime enforcement (see T-5). In the current architecture, the defense-in-depth controls are:

- Agents have no direct network access beyond their MCP servers and Bedrock (no arbitrary outbound HTTP).
- Tool invocations are logged to CloudWatch, producing an audit trail.
- Per-agent IAM runtime roles grant only the specific SSM parameters each agent needs.

Exfiltration through legitimate tool paths (e.g., encoding data in a GitHub comment) is not prevented, and is accepted as an LLM-trust limitation until Cedar enforcement lands.

**T-16 (Mitigated):** DynamoDB encryption-at-rest uses AWS-owned keys by default. The `dispatch-assignments` table has a 30-day TTL on every record (`AttributeName: ttl`, `Enabled: true`), bounding exposure window. Cross-account access is not possible without an IAM principal in the same account.

**T-17 (Open):** `source_context` (GitHub `issue_number`, Asana `task_gid`) is passed through untrusted channels (webhook payloads). If manipulated, the agent posts its output to a different issue or task than the originator. Fix path: validate the target context against the original mention event before posting; cross-check sender and target belong to the same project or repo.

### 3.6 Supply Chain & Build Pipeline

| ID | Threat | Severity | Component | STRIDE | Status |
|----|--------|----------|-----------|--------|--------|
| T-18 | **Compromised base image or dependency in agent container** | **Medium** | C-12 | Tampering | Partially mitigated |
| T-19 | **ECR image tag mutability** | — | C-12 | — | Mitigated |
| T-20 | **GitHub Actions workflow injection** | — | C-1 | Tampering | **Superseded** (dispatch workflow retired) |
| T-31 | **Capability-deployer privileged-IAM surface** | **Medium** | C-17 | Elevation of Privilege | Mitigated |

**T-18 (Partially mitigated):** Every image the shared build pipeline pushes is scanned on push (the `sdlc-agents/*` repos are created with `scanOnPush=true`), and the weekly `capability-rebuilder` rebuild re-scans every active agent's image. Python dependencies are pinned to versions in `requirements.txt` but not hashed. Dependabot opens PRs for updates. Fix path: add `pip install --require-hashes` with a lockfile, and gate the runtime deploy on scan severity.

**T-19 (Mitigated):** ECR repositories for fleet agents (`sdlc-agents/*`) are created with `--image-tag-mutability IMMUTABLE` by the shared build pipeline (`sdlc-agent-builder-${STAGE}` CodeBuild) the first time an agent is built. Images are tagged with a fresh per-build tag only; no `:latest` tag is produced. An attacker with ECR push rights cannot silently overwrite a running image — every push requires a new tag, and the AgentCore runtime is updated explicitly by the `capability-deployer` Lambda against the specific built tag. Operators with pre-existing MUTABLE repos from earlier deploys are not automatically upgraded; delete and recreate for the hardened default.

**T-20 (Superseded):** This threat covered command injection via the untrusted comment body in the `agent-dispatch.yml` GitHub Actions workflow. **That workflow is retired** — the GitHub trigger path is now the `github_webhook.py` Lambda (C-1), which never shell-interpolates the comment body: the body is parsed in Python (a mention regex + a JSON dispatch payload) and the injection-scoring guardrail runs at the Router edge (T-2). There is no GitHub Actions dispatch surface left to inject into. The remaining GitHub-side risk is content-level prompt injection, covered by T-1/T-2.

**T-31 (Mitigated):** The onboarding pipeline deliberately isolates the one genuinely privileged capability — creating IAM roles and AgentCore runtimes — onto the **capability-deployer** Lambda (C-17), which is **invocable only by the CodeBuild build-completion EventBridge rule**, never by the internet-facing dashboard API. The admin API (reachable via the authenticated dashboard) holds only `codebuild:StartBuild`; it cannot create roles, pass roles, or create runtimes. The deployer's own grants are tightly bounded: `iam:CreateRole` is scoped to IAM path `arn:aws:iam::<acct>:role/sdlc-agents/capabilities/*` **and** conditioned on `iam:PermissionsBoundary` equal to the `CapabilityRuntimeBoundary` managed policy — so any role it creates is capped by the boundary (Mantle inference + bearer-token mint, guardrail apply + classic InvokeModel for Titan, DynamoDB, ECR pull, gateway invoke, logs) and can never exceed it. `iam:PassRole` is scoped to the same path and conditioned on `iam:PassedToService = bedrock-agentcore.amazonaws.com`. The deployer is granted no `iam:AttachRolePolicy`, so it cannot attach an arbitrary managed policy — runtime roles get their permissions only via inline `PutRolePolicy` within the boundary ceiling. **This is a NEW privileged surface** relative to the prior architecture (where deploy ran in CI); the controls above are why concentrating it here is safer than the retired CI deploy role: the surface is event-only (not reachable from the API), path-scoped, and boundary-capped. **Residual:** a defect in the deployer's own logic (e.g. building an over-broad inline policy) is bounded by the permissions boundary but not eliminated; the deployer's code is in-repo and reviewed like any Lambda. A compromise of the CodeBuild build (T-18) that produced a malicious image would still be deployed by the deployer — the boundary limits what that image's runtime role can reach, but image provenance is T-18's concern, not T-31's.

### 3.7 Denial of Service & Resource Exhaustion

| ID | Threat | Severity | Component | STRIDE | Status |
|----|--------|----------|-----------|--------|--------|
| T-21 | **Token budget exhaustion** | **Medium** | C-4 | Denial of Service | Open |
| T-22 | **Concurrency slot exhaustion** | **Medium** | C-3 | Denial of Service | Partially mitigated |
| T-23 | **Runaway agent-to-agent chains** | **High** | C-3, C-4 | Denial of Service | Open |

**T-21 (Open):** each agent's capability row declares a `daily_token_budget` (in `limits`) but no runtime enforcement exists. A flood of requests or a prompt-injected loop could run the Bedrock bill up. Fix path: track daily token consumption per agent in DynamoDB and reject dispatches over budget. AWS Service Quotas on Bedrock model invocations is an out-of-band ceiling operators can set.

**T-22 (Partially mitigated):** The Dispatch Router consults DynamoDB for active-assignment counts per agent and rejects dispatches over `max_concurrent`. What's absent: per-user rate limiting. A single user can legitimately fill the concurrency window and block others. Fix path: add a per-sender dispatch-count bucket in DynamoDB with a short rolling window.

**T-23 (Open):** Cross-agent invocation is an **intentional design property** of this fleet, not a threat. Workitems orchestrates Docwriter (and Claude Code) by posting `@docwriter` / `@claude` comments that the dispatch workflow fires on; future handoffs such as Adr → Workitems are anticipated. A blanket "reject bot-authored events" rule would break the primary workflow, so it is explicitly not the control.

The real threat is a **chain that doesn't stop** — a prompt-injected or mis-prompted agent that issues mentions indefinitely, or a bidirectional handoff that fails to terminate. Consequences are compute spend (Bedrock token bill), DynamoDB write pressure on the assignments table, and delayed dispatch for legitimate work as concurrency slots fill.

**Current controls:**
- `authorization.users` allowlists per agent (T-4) — runaway only propagates between agents the operator has explicitly paired.
- `max_concurrent` per agent in the registry, enforced by the Dispatch Router. Caps in-flight work but does not bound total volume over time.
- Mention gating in the webhook receivers (`github_webhook.py` / `asana_webhook.py`) — an event is only forwarded to the Router if it @mentions a known agent token, which bounds the surface but not the volume.

**Recommended mitigation (circuit breaker in the Dispatch Router):** thread a `parent_assignment_id` through dispatch and track three signals in DynamoDB; trip on any of them and emit a CloudWatch alarm:

1. **Chain depth** — reject when the parent chain exceeds a configured depth (e.g. 5). Catches narrow recursion like workitems → docwriter → workitems → docwriter.
2. **Per-agent dispatch rate** — a rolling-window counter keyed on `(agent_id, minute_bucket)`. Rejects past N dispatches/minute. Catches volume-based runaway regardless of origin.
3. **Daily token spend** — actualize the `daily_token_budget` field already declared in the registry (see T-21). Reject past the limit.

The three are complementary: depth handles narrow loops, rate handles fan-out floods, budget is the longer-horizon backstop. Implementation belongs in the Router where all three signals converge.

### 3.8 Security-Scan Exceptions (Accepted with Rationale)

Automated scanners (checkov, semgrep, bandit) flag several patterns in this repository that are intentional design choices for the reference architecture rather than unmitigated risks. They are enumerated here so that operators who re-run the same scans know why these findings are not treated as open work. Each exception should be re-evaluated if the deployment context changes (multi-tenant, regulated workload, customer-managed keys mandate, etc.).

| ID | Finding | Why accepted | If your posture differs |
|----|---------|--------------|-------------------------|
| T-24 | **CKV_AWS_119** — DynamoDB table not encrypted with a customer-managed KMS key | `dispatch-assignments` holds operational state (mention body, assignment status, guardrail trip records) with a 30-day TTL — not long-lived PII or regulated data. AWS-owned keys meet the bar for a reference architecture, avoid key-management surface, and incur no per-request KMS cost. | Swap `SSESpecification` to `KMSMasterKeyId: !Ref <YourCmkKey>` and grant `kms:Decrypt`/`kms:GenerateDataKey` to the Dispatch Router runtime role. |
| T-25 | **CKV_DOCKER_2** — Dockerfiles missing `HEALTHCHECK` instructions | Bedrock AgentCore Runtime manages container lifecycle via its invocation endpoint and internal liveness signals; Docker's `HEALTHCHECK` directive is not consulted by AgentCore. Adding it would give a false impression of active health management without affecting runtime behavior. | Only meaningful if you migrate agents off AgentCore to a runtime that honors Docker healthchecks (ECS, raw Kubernetes); at that point wire a Strands `/health` endpoint and add the directive. |
| T-26 | **CKV_AWS_173** — Lambda environment variables not encrypted with a KMS CMK | The Lambda functions' env vars hold *references* to SSM parameters (names and resource ARNs), not secret values. Actual credentials are fetched from SSM SecureString at invocation time (T-8). A CMK on the env-var block would encrypt public identifiers. | If your account-level policy mandates CMK-everywhere, set `KmsKeyArn` on each `AWS::Serverless::Function` to an existing CMK and grant `kms:Decrypt` to the runtime role. |
| T-27 | **CKV_AWS_120** — API Gateway caching not enabled on the webhook endpoint | The Asana webhook handler verifies HMAC-SHA256 on every inbound payload and routes events to the Dispatch Router asynchronously. Caching would serve cached 200s to replayed or forged payloads and defeat signature verification semantics. | Not recommended to enable. |
| T-28 | **CKV_AWS_117** — Lambda functions not deployed inside a VPC | Both Lambdas (Dispatch Router, Asana webhook) talk only to AWS service endpoints (DynamoDB, SSM, Bedrock AgentCore, Lambda Invoke) and to external HTTPS APIs (Asana, GitHub). There are no private VPC resources to reach. A VPC attachment would add ENI management + cold-start latency with no reachability benefit. | If you introduce a private backend (RDS, internal ALB, VPC endpoint to Bedrock for egress control), attach both Lambdas to a private subnet with NAT egress and add `AWSLambdaVPCAccessExecutionRole`. |

**Mitigated by this same review** (no longer exceptions): CKV_AWS_28 (DynamoDB PITR enabled), CKV_AWS_18/CKV_AWS_21 (S3 access logging + versioning), CKV_AWS_73/CKV_AWS_76 (API Gateway X-Ray + access logs), CKV_AWS_115/CKV_AWS_116 (Lambda reserved concurrency + DLQ), CKV_DOCKER_3 (non-root container user), CKV2_GHA_1 (top-level workflow `permissions: contents: read`).

---

## 4. Trust Boundaries

| Boundary | Components Inside | Components Outside | Controls |
|----------|-------------------|--------------------|----------|
| **AWS Account** | C-1, C-2 through C-8, C-12 through C-17 | C-9/GitHub (external SCM), C-10 (Asana MCP), C-15 Mantle endpoint (AWS-managed) | IAM; webhook HMAC verification at ingress |
| **Webhook edge** | C-1, C-2 (public API Gateway) | GitHub, Asana | HMAC-SHA256 signature verification (fail-closed), async invoke only on verified events |
| **Dispatch Layer** | C-1, C-2, C-3 | C-4 (Agents) | IAM roles, Lambda invoke permissions, scoped `InvokeAgentRuntime`, edge guardrail |
| **Agent Runtime** | Individual agent container | Other agents, Dispatch layer | AgentCore runtime isolation, per-agent boundary-capped IAM roles |
| **Tool-call boundary** | C-14 (Gateway + Cedar engine + interceptor) | C-9, C-10 | Gateway-only (SigV4), Cedar default-deny/forbid-wins, co-repo interceptor, per-owner scoped App tokens |
| **Dashboard control plane** | C-13 (AVP), dashboard query/admin Lambdas | Operators (browser) | Cognito login + AVP `IsAuthorized` (fail-closed); admin API holds only `codebuild:StartBuild` |
| **Build & deploy** | C-12 (ECR), C-16 (CodeBuild), C-17 (capability-deployer) | Developer workstations | CodeBuild service role (in-account image build), scan-on-push, immutable ECR tags; privileged IAM isolated on the event-only deployer (path-scoped + permissions boundary) |

---

## 5. Risk Summary

Risk is expressed as the residual exposure given current controls. Mitigated threats are not re-scored.

| Severity | Count | Threats |
|----------|-------|---------|
| **Critical (Partially mitigated)** | 1 | T-1 |
| **High (Accepted)** | 1 | T-15 |
| **High (Partially mitigated)** | 3 | T-2, T-3, T-5 |
| **High (Open)** | 1 | T-23 |
| **Medium (Open or Partial)** | 8 | T-11, T-13, T-17, T-21 (open); T-12, T-18, T-22, T-30 (partial) |
| **Medium (Mitigated)** | 1 | T-31 |
| **Low (Accepted with rationale)** | 5 | T-24, T-25, T-26, T-27, T-28 |
| **Low** | 3 | T-10, T-14, T-16 |
| **Mitigated (Not scored)** | 7 | T-4, T-6, T-8, T-9, T-19, T-29 |
| **Superseded (retired path)** | 2 | T-7 (GitHub OIDC), T-20 (Actions workflow injection) |

---

## 6. Recommended Next Work

Roadmap items ordered by leverage:

1. **Cedar runtime enforcement (T-5, T-15)** — the highest-value *structural* control. Intercepting tool calls and enforcing per-agent allow/deny rules bounds prompt-injection blast radius, prevents exfiltration via unauthorized tools, and makes the `cedar/*.cedar` files load-bearing rather than aspirational. Complements the now-shipped Guardrails layer: Guardrails reduces the probability of subversion; Cedar bounds the damage if it occurs.
2. **Dispatch circuit breaker (T-23, T-21, T-22)** — thread `parent_assignment_id` through dispatch; enforce chain-depth, per-agent rolling-rate, and daily-token-budget limits in the Dispatch Router with CloudWatch alarms on trip. Closes the primary DoS surface of a by-design multi-agent topology.
3. **Input validation on dispatch (T-1, T-2, T-17)** — lightweight classifier-based pre-filter in the Dispatch Router layered before Guardrails; cross-check that agent output targets the originating context.
4. **API Gateway throttling (T-13)** — usage-plan throttling and optional WAF for IP-based limits.
5. **GitHub App over PAT (T-11)** — default docs and skills to App installation tokens.
6. **Dependency hash-pinning (T-18)** — `pip install --require-hashes` with a lockfile.

---

## 7. Assumptions & Scope

- Covers the fleet as shipped: four agents (workitems, researcher, docwriter, adr), Dispatch Router, GitHub App + Asana webhooks, the AgentCore Gateway (gateway-only tool access), the AVP-authorized dashboard, and the UI-driven onboarding pipeline (CodeBuild + capability-deployer). Slack integration, AgentCore Memory, AgentCore Identity, and Feedback/UAT agents are out of scope — they are not yet implemented.
- Single AWS account + single region deployment. Multi-account or cross-region introduces additional trust boundaries not analyzed here.
- LLM model behavior (hallucinations, jailbreaks, adversarial-input sensitivity) is treated as a baseline risk of using foundation models. Mitigations focus on constraining what the agent can *do*, not on preventing the model from generating bad outputs.
- GitHub MCP and Asana MCP servers are treated as trusted third-party services. Their internal security posture is out of scope.
- This fleet is intended as a reference architecture. Operators deploying it against sensitive production repositories should revisit every **Accepted** and **Open** finding and make their own risk decisions before launch.

---

## Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2026-07-20 | 1.8 | Ship-accurate refresh for the current architecture. **Component inventory:** C-1 is now the GitHub App webhook Lambda (was `agent-dispatch.yml`); C-11 (GitHub OIDC provider) **retired**; C-4 model → Claude Sonnet 5 via Mantle; C-9 → SCM broker gateway target; new C-13 (AVP dashboard-API policy store), C-14 (AgentCore Gateway + Cedar engine + interceptor), C-15 (Bedrock Mantle + per-repo projects), C-16 (shared CodeBuild), C-17 (capability-deployer + `CapabilityRuntimeBoundary`). **Data flows** rewritten for the webhook trigger path, gateway-only tool calls, and Mantle model calls (bearer token + guardrail headers + `OpenAI-Project`); added DF-14/15/16 for the dashboard + onboarding pipeline. **Threats:** T-5 → Partially mitigated (Gateway Cedar engine enforces); T-7 and T-20 → **Superseded** (OIDC/CI dispatch retired); new T-29 (AVP fail-closed API authz), T-30 (GitHub App webhook HMAC — replaces the OIDC trust boundary), T-31 (capability-deployer privileged-IAM isolation: event-only, path-scoped, boundary-capped). Runtime guardrail narrative updated for Mantle-header attachment + fail-closed `build_model`. |
| 2026-05-05 | 1.7 | Checkov / semgrep scan pass (Kai Xu review). Hardened CFN: DynamoDB PITR, S3 versioning + access logs, API Gateway X-Ray + access logs, Lambda reserved concurrency + SQS DLQ; Dockerfiles switched to non-root `agent` user; all workflows given top-level `permissions: contents: read`. New §3.8 documents T-24..T-28 — accepted scanner findings (CKV_AWS_119, CKV_DOCKER_2, CKV_AWS_173, CKV_AWS_120, CKV_AWS_117) with rationale and upgrade paths. |
| 2026-05-05 | 1.6 | Security review fixes: T-4 Asana sender is now the user `.gid` (was the self-editable display name — a HIGH-severity auth bypass); Router rejects unresolved sender sentinels ("", "unknown") as defense-in-depth. §3.1 narrative now states the shipping guardrail posture (`InputStrength: MEDIUM`, `OutputStrength: NONE`) — prior text implied HIGH/HIGH. |
| 2026-05-05 | 1.5 | T-1/T-2/T-3 flipped from Accepted to Partially mitigated — Bedrock Guardrails (`PROMPT_ATTACK`) enforced at the Dispatch Router edge and on every agent's `InvokeModel` call. Section 6 roadmap re-ordered: Cedar enforcement now #1. |
| 2026-05-01 | 1.4 | Added Amazon Bedrock Guardrails (prompt-attack filter) as the recommended near-term defense for T-1/T-2/T-3; promoted to #1 in Section 6 roadmap ahead of Cedar enforcement |
| 2026-05-01 | 1.3 | T-8 flipped to Mitigated (PAT fetch moved to invocation scope); T-9 flipped to Mitigated (Lambda loses steady-state `ssm:PutParameter`; operator bootstrap script mediates Asana handshake); T-1 Authorization bullet corrected to reference T-4 allowlist |
| 2026-05-01 | 1.2 | T-4 flipped to Mitigated (registry defaults to `users: []`, router fails closed); T-23 reframed as intentional cross-agent design with circuit-breaker as the recommended control |
| 2026-05-01 | 1.1 | Reframed as living document; statuses added (Mitigated / Partial / Accepted / Open) for each threat |
| 2026-05-01 | 1.0 | Initial assessment of v1 fleet |

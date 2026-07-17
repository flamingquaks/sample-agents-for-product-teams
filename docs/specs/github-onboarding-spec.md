# GitHub Repo Onboarding & GitHub App Credential Model
## Supporting individual and organization repos with a bounded, per-owner credential

Status: **Direct mode complete.** GitHub App onboarding (manifest flow,
per-owner install verification) AND the agent-runtime + reply-Lambda credential
cutover are built behind `GITHUB_AUTH_MODE=app` (default `pat`): in app mode the
three GitHub agents and the dispatch reply Lambda mint per-owner installation
tokens for the dispatched repo's owner instead of reading the shared PAT
(`agents/shared/tools/github_app.py`, `infra/dispatch/github_app.py`), with IAM +
env wired in the template, bootstrap, and the agent deploy workflow. Remaining:
the §3.6 broker Lambda target for **gateway** mode, PAT decommission once all
stages run `app` (§6), and GitLab/Bitbucket providers.
Owner: fleet infra. Related: `docs/threat-model.md` T-11, `docs/roadmap.md`,
`skills/sdlc-agents-connect-github/SKILL.md` (Path B).

---

## 1. Problem

The fleet must let an admin onboard **any GitHub repo the fleet is entitled to
act in — whether the owner is an individual user (`alice/project`) or an
organization (`acme/service`)** — and give each agent exactly the GitHub access
it needs (issue comments, PR read/create, code read, code write) and no more.

Two things block this today:

1. **One shared, over-broad credential.** Every GitHub call site — the three
   agents, the dispatch reply Lambda, and the Gateway outbound role — reads a
   single SSM SecureString PAT at `/sdlc-agents/github-mcp-token`
   (`agents/*/tools/github_mcp.py:25-40`, `infra/dispatch/reply.py:38`,
   `infra/foundation/template.yaml:307,334,895-904`). A `repo`-scoped PAT grants
   write to **every** repo its owner can reach, not just onboarded ones — the
   Elevation-of-Privilege residual risk called out in threat-model **T-11**
   ("PAT scope may be overly broad", *Partially mitigated*). A single personal
   token also cannot cleanly span *both* an individual's repos and an org's
   repos without belonging to a human who is a member of both.

2. **Onboarding trusts a typed string.** `admin._valid_repo` (`admin.py:108`) is
   a *syntax* check (`owner/repo`, legal chars) explicitly documented as "**Not
   a GitHub existence check**." Nothing verifies the repo exists, that the fleet
   can reach it, or whether the owner is a User or an Organization. An admin can
   onboard `typo/repo` and it silently becomes `active`.

**Decision (this spec):** transition the credential model from one fleet PAT to
a **GitHub App with per-owner installation tokens**, and make onboarding
**verify installation** before activating a repo. The PAT is retired, not kept
as a fallback — it is "too restrictive" (a single owner scope) and too broad (no
per-repo bound) at the same time.

### Why a GitHub App is the right model

| | Individual repo (`alice/project`) | Org repo (`acme/service`) |
|---|---|---|
| **Trust anchor** | App **installed on the user account** | App **installed on the organization** |
| **Credential minted per call** | Installation access token, scoped to the repos selected in *that* installation, expires in ~1h | Same |
| **Spans both?** | Yes — each owner is a **separate installation** with its own `installation_id`; the fleet holds many, keyed by owner | |
| **Fine-grained permissions** | Contents, Issues, Pull requests, Metadata — set once on the App, apply to every install | |

An installation token is bounded by construction: it can only touch repos in
that installation, and only with the App's permission set. That closes T-11 at
the credential layer, complementing (not replacing) the Gateway Cedar allowlist
at the tool-call layer. The two become defense-in-depth: **credential ceiling ∩
policy floor**.

---

## 2. Current state (for reviewers)

- **Onboarding record** (`config_store.put_repo`, DynamoDB): `pk="repo#<owner/repo>"`,
  fields `kind, repo, enabled, multi_repo_eligible, onboarded_by, onboarded_at,
  status(pending|active)`. Repo is lowercased/normalized. **Org-agnostic
  already** — `owner` is just a string; nothing records whether it's a user or org.
- **Two enforcement planes** read the record: dispatch (`fleet_config.is_repo_allowed`)
  and Gateway Cedar (`fleet_policy` via `policy_sync`). Both stay as-is here.
- **Per-agent GitHub access already exists** and is authoritative in
  `fleet_policy.AGENT_TOOL_GRANTS` + `cedar/*.cedar`:

  | Agent | Reads | Writes | Code write? |
  |---|---|---|---|
  | **workitems** | issues, PRs, milestones | `create_issue`, `update_issue`, `add_issue_comment`, `add_labels_to_issue` | No |
  | **docwriter** | file contents, `search_code`, PRs, issues, commits | + `create_pull_request`, `create_or_update_file`, `push_files`, `create_branch` | **Yes (doc PRs)** |
  | **adr** | file contents, `search_code`, issues, PRs, commits | `add_issue_comment`, `add_labels_to_issue` | No |
  | **researcher** | — | — | **No GitHub at all (Asana-only)** |

  Destructive tools (`delete_file`, `merge_pull_request`, `delete_branch`) are
  **unconditionally forbidden** fleet-wide (`fleet_policy.DESTRUCTIVE_TOOLS`).

- **The App path is documented but NOT implemented.**
  `skills/sdlc-agents-connect-github/SKILL.md` Path B describes App registration
  + three SSM params (`github-app-id`, `github-app-installation-id`,
  `github-app-private-key`) and claims "existing tool code … reads either SSM
  shape." **That claim is false** — `github_mcp.py` only reads the single PAT
  param; there is no JWT-minting or installation-token code anywhere. The skill
  also stores a **single** `installation-id`, which still can't span two owners.

---

## 3. Target architecture

### 3.1 The App and its permissions

One GitHub App per stage (e.g. `SDLC Agent Fleet (staging)`), registered once by
an org/account admin (a manual GitHub step, outside this repo — a documented
prerequisite, like the Cognito pool). Repository permissions map to the union of
what agents need (the ceiling; Cedar narrows per agent below it):

| Permission | Level | Why (agent) |
|---|---|---|
| Metadata | Read | required baseline |
| Issues | Read & Write | workitems, docwriter, adr (comments/labels) |
| Pull requests | Read & Write | docwriter (create PR), workitems/adr (read) |
| Contents | Read & Write | docwriter (`push_files`, `create_or_update_file`, `create_branch`) |

Note the App permission set does **not** grant merge/close/delete beyond what
Contents/PR R&W implies; the Cedar `DESTRUCTIVE_TOOLS` forbid remains the
authoritative block on merge/delete/close, since a GitHub App with PR write
*can* technically merge. **The Cedar destructive forbid is still load-bearing —
the App permissions do not replace it.**

### 3.2 Credential storage (Secrets Manager for the key, SSM for IDs)

Retire `/sdlc-agents/github-mcp-token`. The store is chosen per-value by
sensitivity + lifecycle, not one-size-fits-all:

| Value | Store | Why |
|---|---|---|
| App **private key** (PEM) | **Secrets Manager** `sdlc-agents/github-app/private-key` | The one true long-lived secret. Secrets Manager gives native rotation (versioned `AWSCURRENT`/`AWSPENDING` staging labels — resolves O-5), per-secret **resource policies** (lock read to exactly the agent/reply/admin roles, which SSM's identity-only model can't), and staged versions. Read via `secretsmanager:GetSecretValue`. |
| `github-app-id` | **SSM String** (plain) `/sdlc-agents/github-app-id` | Not a secret — an integer App ID. SecureString/SM would be over-engineering. |
| `github-app-slug` | **SSM String** (plain) `/sdlc-agents/github-app-slug` | Not a secret — the public app slug, used to build the install deep-link (O-3). |
| `installation_id` (per owner) | **DynamoDB** | Not a secret — config; resolved at onboard, keyed per owner (see below). |

**Why the split rather than all-Secrets-Manager or all-SSM:** the existing fleet
keeps its Asana PAT / webhook secret and the old GitHub PAT in **SSM
SecureString** (`reply.py`, `asana_webhook.py`, `template.yaml`), so SM is *new*
to this codebase — the private key is the value that actually justifies it
(rotation + resource policy), and it matches the store the `ai-dlc-platform`
reference used for the same App secrets. The non-secret IDs stay in cheap SSM
String; putting a public app-id/slug in Secrets Manager would be waste. This
does introduce a **second secret store** (SM for the App key, SSM for the Asana
secrets) — a deliberate, documented choice (note in threat-model), not drift.
Fleet-wide standardization on one store (migrating the Asana secrets to SM too)
is a possible later workstream, out of scope here.

**Installation IDs are NOT a single param.** They are **per-owner**, resolved at
onboard time and stored in DynamoDB (§3.3) — the key departure from the SKILL.md
Path B design and the thing that lets one App span many individual + org owners.

> Open question O-1: store `installation_id` per repo record, or dedupe into a
> per-owner record (`pk="owner#<owner>"`)? Per-owner is cleaner (one install
> serves N repos of that owner) but adds a record type. Leaning per-owner —
> see §7.

### 3.3 Onboarding record — new fields

`put_repo` gains (all resolved server-side at onboard, never client-supplied):

```
owner_type:      "User" | "Organization"   # from GET /users/{owner}
installation_id: "<int>"                    # the App's install on that owner
install_verified_at: <epoch>                # when we last confirmed reachability
```

`_valid_repo` stays as the injection-safe syntax gate (owner/repo flows into
Cedar literals — unchanged). Existence/installation verification is a **new,
separate step** (§4), not folded into `_valid_repo`.

### 3.4 Token minting

New module `agents/shared/tools/github_app.py` (and a mirror usable by the
reply Lambda — see §5):

```
mint_installation_token(installation_id) -> (token, expires_at)
  1. jwt = sign({iss: app_id, iat, exp:+9min}, private_key_pem, RS256)
  2. POST https://api.github.com/app/installations/{id}/access_tokens
         Authorization: Bearer <jwt>
  3. cache (token, expires_at) keyed by installation_id; refresh at exp-60s
```

- The private-key PEM is read from **Secrets Manager**
  (`sdlc-agents/github-app/private-key`) via `secretsmanager:GetSecretValue`, and
  the App ID from SSM String (`/sdlc-agents/github-app-id`). The key is cached
  in-memory (short TTL) so signing doesn't call SM on every mint.
- Reuses the existing per-invocation-cache discipline the code already favors
  (see `github_mcp._cached_token`); token cache keyed by `installation_id`,
  honoring `expires_at`.
- `PyJWT` + `cryptography` are new agent dependencies (RS256 signing); the code
  gains a `boto3.client("secretsmanager")` path (first Secrets Manager use in
  this repo — see §3.2).

### 3.5 How each call site changes

| Call site | Today | Target |
|---|---|---|
| **Agents, direct mode** (`github_mcp.github_bearer_token`) | reads PAT | ✅ **DONE** — in app mode, `github_app.token_for_dispatch(owner/repo)` mints a token for the **dispatched repo's owner**; `agent.py` resolves `dispatch_repo` before building the GitHub client and passes it in. PAT path preserved as default. |
| **Agents, gateway mode** | Gateway holds PAT for outbound | Route through a **broker Lambda target** that mints the per-owner token per call — see §3.6 (this replaces the old "static gateway credential" dead-end; O-2 resolved). **Not yet built** — keep GitHub in direct mode. |
| **Dispatch reply Lambda** (`reply.post_github_comment`) | reads PAT | ✅ **DONE** — `reply._github_token(repo)` mints per-owner via `infra/dispatch/github_app.py` in app mode, PAT otherwise. |
| **Bootstrap + template IAM/env** | grants `github-mcp-*` | ✅ **DONE** — agent runtime roles (bootstrap `GITHUB_AGENTS`) + the dispatch router role get `secretsmanager:GetSecretValue` (key), `ssm:GetParameter` (app-id), `dynamodb:GetItem` (per-owner install record); `GITHUB_AUTH_MODE`/param/table env set on the router (template) and agents (deploy-agent.yml, from stack outputs). A deploy Rule requires `DeployDashboard=true` when `GitHubAuthMode=app`. |

> O-2 (Gateway outbound per-owner token) — **RESOLVED** (see §3.6). The static
> per-target credential genuinely can't vary per call, and AgentCore Identity
> can't mint GitHub App installation tokens (it's OAuth2/API-key-shaped only).
> The answer is NOT per-owner targets/gateways (a namespace + tool-listing
> multiplier) and NOT AgentCore Identity: it's a single **broker Lambda target**
> that mints the right per-owner/per-provider credential inside our own code, per
> call. Verified against AWS docs (2026): Lambda targets receive the resolved
> tool args + `bedrockAgentCoreToolName`, define their own tool schema, make
> arbitrary outbound calls, use the `GATEWAY_IAM_ROLE` credential model (designed
> for "our code holds the downstream credential"), and Cedar still evaluates.

### 3.6 Gateway mode — the SCM broker Lambda target

The gateway's value here is **not** just the Cedar chokepoint: it's a single MCP
endpoint that fronts **GitHub, GitLab, and Bitbucket uniformly** with one tool
surface, plus native per-tool observability. GitHub App installation tokens are
per-owner and minted per call, which a static per-target credential can't
express — so instead of pointing the gateway at GitHub's MCP server directly,
point it at **one broker Lambda target** (`agents/.../scm_broker` or
`infra/gateway/scm_broker`, TBD) that:

- **Exposes a provider-agnostic tool schema** (one `toolSchema` on the target):
  `create_issue`, `add_issue_comment`, `add_labels`, `get_issue`, `list_issues`,
  `get_pull_request`, `list_pull_requests`, `create_pull_request`,
  `get_file_contents`, `create_or_update_file`, `push_files`, `create_branch`,
  `list_commits`, `search_code`. **Curated to what the agents' Cedar grants
  actually use** (`fleet_policy.AGENT_TOOL_GRANTS`) — NOT a 1:1 mirror of the
  full GitHub MCP surface. *(Decision to confirm: curated set vs full mirror.)*
- **Dispatches on `bedrockAgentCoreToolName`** (`<Target>___<tool>`), resolving
  the provider from the call's `owner/repo` via the config store (the repo/owner
  records already know owner + installation_id; a `provider` field is added when
  GitLab/Bitbucket land).
- **Mints the per-call credential in our code** — GitHub via `github_client`'s
  App-JWT→installation-token flow; GitLab/Bitbucket via their own token model
  later. This is the same minting the direct-mode agents use, moved server-side.
- **Calls the provider's REST API** and returns the tool result as JSON.
- **Logs every tool call** (agent, tool, owner/repo, provider, outcome, latency)
  — on top of the gateway's native metrics/logs/spans (`Name`=tool dimension).

Credential model: the Lambda target uses `GATEWAY_IAM_ROLE` only (the gateway
invokes it; the Lambda itself reads the SM private key + mints tokens). NO
outbound OAuth/API-key credential provider on the target.

**Caller identity caveat (verified):** a Lambda *target* does NOT natively
receive the Cedar principal / inbound identity — only gateway/target/tool IDs.
If the broker needs "which agent is calling" (e.g. to scope beyond what Cedar
already enforces), add a **REQUEST interceptor** (customer Lambda, runs before
Cedar) that injects the identity into the request. Cedar's per-agent
`permit`s remain the authoritative per-agent tool scope, so the broker may not
need identity at all for v1 — confirm during implementation.

**Why not the alternatives** (recorded so we don't relitigate):
- *Per-owner gateway targets / per-owner gateways* — a gateway scales to many
  targets fine, but using a target as a per-tenant credential holder multiplies
  the Cedar action namespace (`<owner-target>___<tool>`) and re-lists the whole
  tool set per target. One broker target avoids both.
- *AgentCore Identity* — OAuth2/API-key-shaped only; can't mint GitHub App
  installation tokens (§ research). It DOES fit GitLab/Bitbucket OAuth, so it
  may serve those providers' credential storage even though the broker owns the
  GitHub App path.

**Rollout:** direct mode (built) is unaffected and ships first. The broker is a
distinct, later workstream (its own tool-surface implementation per provider);
gateway mode stays `LOG_ONLY` until it lands. `github_client`'s minting is shared
between direct mode and the broker (extract to `agents/shared` or an importable
module).

---

## 4. Onboarding flow (target)

```
Admin clicks "Onboard repository" (modal)                     [built]
  → enters owner/repo  (or picks from installable repos — stretch, §7)
POST /admin/repos { repo }
  1. _valid_repo(repo)                          → 400 on bad syntax   [exists]
  2. resolve owner_type:  GET /users/{owner}     (App JWT auth)        [new]
  3. find installation:   GET /users/{owner}/installation  or
                          GET /orgs/{owner}/installation               [new]
       - not installed → 409 + install deep-link:
         https://github.com/apps/<app-slug>/installations/new         [new]
  4. verify repo in installation & reachable:
         GET /repos/{owner}/{repo}  with a minted installation token   [new]
       - 404/no access → 409 "repo not covered by the installation"    [new]
  5. put_repo(... owner_type, installation_id, install_verified_at,
               status="active")                                        [extended]
  6. _sync_repo_policy()  (unchanged; enforcement-aware rollback)      [exists]
```

Steps 2-4 are the new verification gate. Failures return actionable errors
(install link, or "not covered") — never a silent `active` onboard of an
unreachable repo. This directly fixes the "trusts a typed string" gap.

### UX (modal, already built)

The modal from the just-shipped change is the entry point. Additions:
- On a 409 "not installed," render the **install deep-link** as a button in the
  modal (open in new tab), and a "Re-check" action that re-runs verification
  after the admin installs — so onboarding is a guided loop, not a dead end.
- Show resolved `owner_type` (User/Org) as a read-only chip after verification,
  so the admin can confirm they onboarded the right owner.

---

## 5. Affected files (implementation preview — not done in this spec)

| File | Change |
|---|---|
| `agents/shared/tools/github_app.py` | **new** — JWT sign + installation-token mint + cache |
| `agents/*/tools/github_mcp.py` (×3) | replace `get_github_token()` PAT read with per-owner token mint; drop the identical triplicate in favor of the shared module |
| `agents/*/agent.py` (×3) | pass dispatched `owner` to the token minter |
| `infra/dispatch/reply.py` | mint per-repo token instead of PAT read |
| `infra/dashboard/admin.py` | POST /admin/repos: add steps 2-4 verification; new 409 shapes |
| `infra/dashboard/config_store.py` | `put_repo` accepts + stores `owner_type`, `installation_id`, `install_verified_at` |
| `infra/dashboard/github_client.py` | **new** — App-JWT'd GitHub REST calls for owner-type + installation lookup |
| `infra/foundation/template.yaml` | **Secrets Manager** secret `sdlc-agents/github-app/private-key` (empty, populated out-of-band or at App registration) + `secretsmanager:GetSecretValue` grants on admin/reply/agent roles (optionally a resource policy scoping the secret to just those roles); **SSM String** params `github-app-id` + `github-app-slug` + read grants; drop the `github-mcp-token` SSM param + grants; admin role needs outbound GitHub reachability (calls api.github.com — no IAM, just egress) |
| `scripts/bootstrap.py` | `AGENT_SSM` / `AGENT_REQUIRED_SSM`: drop `github-mcp-*`; add the app-id/slug SSM reads + the Secrets Manager `GetSecretValue` grant for the private key; preflight probes the new secret + params |
| `dashboard/src/AdminView.tsx` | install-deep-link + re-check in the modal; owner_type chip |
| `skills/sdlc-agents-connect-github/SKILL.md` | correct the false "reads either shape" claim; document per-owner installs; retire PAT path |
| `docs/threat-model.md` | T-11 → **Mitigated** once shipped (credential now bounded) |

---

## 6. Migration & rollout

1. **Additive first.** Ship `github_app.py` + verification reading the new SSM
   params, while `/sdlc-agents/github-mcp-token` still exists. Onboarding starts
   requiring an installation; existing `active` repos are backfilled (§ below).
2. **Backfill** existing repo records: a one-shot that resolves `owner_type` +
   `installation_id` for each already-onboarded repo (fails loudly for any repo
   the App isn't installed on — the admin gets a list to install).
3. **Cut over** each call site to token minting behind a flag
   (`GITHUB_AUTH_MODE=app|pat`), default `pat` → flip to `app` per stage after
   verifying live. ✅ **DONE for direct mode** — the three GitHub agents and the
   reply Lambda mint per-owner tokens in app mode (gateway mode still pending the
   §3.6 broker). Flip a stage by setting `GitHubAuthMode=app` on the foundation
   stack and the `GITHUB_AUTH_MODE=app` repo var for agent deploys.
4. **Retire the PAT**: delete the SSM param + IAM grants once all stages are on
   `app` and the gateway path (§3.6) is confirmed. Still pending — the PAT
   remains the default and the fallback while `app` rolls out per stage.
5. Gateway mode lags direct mode: it needs the broker Lambda target (§3.6), a
   separate workstream. Until it ships, gateway mode stays `LOG_ONLY` and GitHub
   calls that route through the gateway use whatever outbound cred the target has
   — so keep GitHub in **direct mode** until the broker lands.

---

## 7. Open questions

- **O-1** — **RESOLVED + implemented.** Per-owner install record
  (`pk="owner#<owner>"`, `config_store.get/put_installation`); repo rows carry
  `installation_id`. One install serves all of an owner's repos.
- **O-2** — **RESOLVED** (§3.6): single broker Lambda target mints per-owner/
  per-provider tokens per call. Not blocking gateway mode's *design* anymore;
  the broker is its own implementation workstream. Direct mode already ships.
- **O-3** — **RESOLVED + implemented.** Slug stored in SSM String
  `/sdlc-agents/${Stage}/github-app-slug`, written by the manifest exchange;
  `github_client.install_url()` builds the deep-link from it.
- **O-4** Rate limits: installation tokens have per-install rate limits;
  per-owner minting + caching should stay well under, but worth noting for
  many-owner fleets.
- **O-5** Private-key rotation: PEM in Secrets Manager — use its native rotation
  (a rotation Lambda that registers a new GitHub App private key and stages it
  `AWSPENDING` → `AWSCURRENT`), and restrict who can `PutSecretValue` in steady
  state (should be no one, like the Asana
  webhook secret pattern).

---

## 8. Out of scope

- Slack/Jira credential models (separate).
- Changing the Cedar per-agent tool grants or the destructive forbid — those
  stay exactly as they are; this spec only changes *how the credential is
  obtained*, not *what each agent may do*.
- GitHub webhook-based dispatch (the App's webhook is disabled;
  `agent-dispatch.yml` remains the mention path).

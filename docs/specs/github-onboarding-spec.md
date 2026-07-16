# GitHub Repo Onboarding & GitHub App Credential Model
## Supporting individual and organization repos with a bounded, per-owner credential

Status: **Draft** — for review. No code changes yet.
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

### 3.2 Credential storage (SSM)

Retire `/sdlc-agents/github-mcp-token`. New params:

| Param | Type | Scope |
|---|---|---|
| `/sdlc-agents/github-app-id` | String | one, fleet-wide |
| `/sdlc-agents/github-app-private-key` | SecureString (PEM) | one, fleet-wide |

**Installation IDs are NOT a single SSM param.** They are **per-owner**, resolved
at onboard time and stored on the DynamoDB repo record (§3.3). This is the key
departure from the SKILL.md Path B design and the thing that lets one App span
many individual + org owners.

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

- Reuses the existing per-invocation-cache discipline the code already favors
  (see `github_mcp._cached_token`); cache keyed by `installation_id`, honoring
  `expires_at`.
- `PyJWT` + `cryptography` are new agent dependencies (RS256 signing).

### 3.5 How each call site changes

| Call site | Today | Target |
|---|---|---|
| **Agents, direct mode** (`github_mcp.get_github_token`) | reads PAT | `github_app.mint_installation_token(installation_id)` for the **dispatched repo's owner**; the dispatch context already carries `owner/repo` (`docwriter/project_config.py`), so the agent knows which installation to mint for |
| **Agents, gateway mode** | Gateway holds PAT for outbound | Gateway outbound auth must mint per-owner tokens too — **see O-2**, this is the hardest piece |
| **Dispatch reply Lambda** (`reply.post_github_comment`) | reads PAT | mint token for the repo it's replying to (it has `repo`); needs its own copy of minting logic + SSM read of app-id/private-key |
| **Bootstrap SSM grants** (`AGENT_SSM`, `bootstrap.py:183`) | grants `github-mcp-*` | grant `github-app-*`; the current prefix does **not** match the new param names, so roles can't read them until updated |

> Open question O-2 (Gateway outbound): the AgentCore Gateway target holds one
> outbound credential for the GitHub MCP endpoint. Per-owner installation tokens
> mean the correct token depends on the *call's* target repo, which the static
> gateway target credential can't express. Options: (a) short-term, install the
> App on all onboarded owners and keep a single broad-ish install token as the
> gateway outbound cred (weaker bound, but still App-scoped); (b) investigate
> whether the gateway target supports a credential-provider hook that can select
> by request; (c) keep the *per-owner* bound only in direct mode and document
> gateway mode as a coarser bound until AgentCore supports dynamic outbound
> creds. **This needs an AgentCore Gateway capability check before we commit.**
> Recall from prior work: agentcoreRuntime HTTP targets reject
> `credentialProviderConfigurations` — so dynamic per-call outbound creds may
> not be available and (a)/(c) may be forced.

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
| `infra/foundation/template.yaml` | SSM param resources + IAM: admin/reply/agent roles read `github-app-*`; drop `github-mcp-token` grants; admin role needs outbound GitHub reachability (it calls api.github.com — no IAM, just egress) |
| `scripts/bootstrap.py` | `AGENT_SSM` / `AGENT_REQUIRED_SSM`: `github-mcp-*` → `github-app-*`; preflight probes new params |
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
   verifying live.
4. **Retire the PAT**: delete the SSM param + IAM grants once all stages are on
   `app` and the reply Lambda + gateway path are confirmed.
5. Gateway mode (O-2) may lag direct mode — document the bound difference until
   resolved.

---

## 7. Open questions

- **O-1** Installation ID storage: per-repo record vs. per-owner record
  (`pk="owner#<owner>"`). *Recommendation: per-owner* — one install serves all
  of an owner's onboarded repos; repo record references the owner. Reduces
  redundant GitHub calls and makes re-check cheap.
- **O-2** Gateway outbound per-owner token (§3.5) — **blocking for gateway
  mode**; needs an AgentCore capability check. Direct mode is unaffected.
- **O-3** App slug for the install deep-link — needs to be recorded at App
  registration and stored (SSM String `/sdlc-agents/github-app-slug`?) so the
  admin API can build the link.
- **O-4** Rate limits: installation tokens have per-install rate limits;
  per-owner minting + caching should stay well under, but worth noting for
  many-owner fleets.
- **O-5** Private-key rotation: PEM in SSM SecureString; document rotation and
  who can `PutParameter` (should be no one in steady state, like the Asana
  webhook secret pattern).

---

## 8. Out of scope

- Slack/Jira credential models (separate).
- Changing the Cedar per-agent tool grants or the destructive forbid — those
  stay exactly as they are; this spec only changes *how the credential is
  obtained*, not *what each agent may do*.
- GitHub webhook-based dispatch (the App's webhook is disabled;
  `agent-dispatch.yml` remains the mention path).

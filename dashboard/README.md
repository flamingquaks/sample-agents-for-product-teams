# Fleet Monitoring Dashboard (SPA)

A React + Vite single-page app that gives operators a fleet-wide view of agent
runs: live status, filtering, per-run trace references, and (Phase 4) run detail
and cross-agent traceability. It reads the operator-authorized query API
(`infra/dashboard/`) and authenticates operators via the Cognito Hosted UI.

This is the **frontend only**. The API, Cognito pool, and (Phase 5) the
CloudFront/S3 hosting are provisioned by the foundation SAM stack behind the
`DeployDashboard=true` flag.

## Architecture notes

- **One build, any stack.** The bundle reads runtime config from `/config.json`
  at startup (written at deploy time from the stack outputs — Phase 5), falling
  back to Vite env vars for local dev. Nothing stack-specific is baked into the
  build. See `src/config.ts`.
- **Auth:** Authorization Code + PKCE against the Cognito user pool via
  `react-oidc-context`. The access token carries the `cognito:groups` claim the
  API checks; tokens are held in `sessionStorage`. See `src/auth.ts`.
- **Live updates:** adaptive polling (fast while runs are active, idle
  otherwise; pauses on a hidden tab) — mirrors the `bgagent watch` cadence. No
  websockets. See `src/hooks.ts`.
- **Data-driven trace chips:** whatever `trace_refs` keys a run carries are
  rendered as chips, so a new integration's dimension shows up with no UI change.

## Develop against a real dev stack

Deploy the foundation stack with `DeployDashboard=true`, then:

```bash
cd dashboard
cp .env.example .env.local     # fill in from the stack outputs (see the file)
npm install
npm run dev                    # http://localhost:5173
```

`http://localhost:5173/` must be registered as a callback + logout URL on the
Cognito app client (the template seeds it). Your Cognito user must be in the
`operators` group or the API returns 403.

## Build

```bash
npm run build        # tsc typecheck + vite production build → dist/
npm run preview      # serve the built bundle locally
```

`dist/` is what the deploy step (Phase 5) uploads to S3 behind CloudFront.

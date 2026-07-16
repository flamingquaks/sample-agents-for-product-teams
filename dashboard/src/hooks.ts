// Small hooks shared by the views: an API client bound to the current auth
// token, and an adaptive polling loop.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DependencyList } from "react";
import { useAuth } from "react-oidc-context";
import { DashboardApi } from "./api";
import type { AppConfig } from "./config";

/**
 * A DashboardApi bound to the live Cognito ID token. Memoized on the token so a
 * token refresh produces a client that sends the new token, but re-renders don't
 * churn the instance.
 *
 * We send the ID token, not the access token: the REST API's COGNITO_USER_POOLS
 * authorizer rejects this pool's access token with 401 (it carries no
 * resource-server scope), while the ID token validates and carries the
 * `cognito:groups` claim the operator/admin checks read. Sending the access
 * token produced a 401 → re-login → redirect loop.
 */
export function useApi(config: AppConfig): DashboardApi {
  const auth = useAuth();
  const token = auth.user?.id_token ?? null;
  return useMemo(
    () => new DashboardApi(config.apiBaseUrl, () => token),
    [config.apiBaseUrl, token],
  );
}

/** Polling cadence (ms): fast while runs are active, backing off when idle —
 *  same shape the CLI `bgagent watch` uses. */
const FAST_MS = 5_000;
const IDLE_MS = 20_000;

interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Manually trigger an immediate refresh (and keep the loop running). */
  refresh: () => void;
}

interface PollOptions<T> {
  /** Inspects the latest data to decide the cadence: true → fast, false → idle. */
  isActive: (data: T) => boolean;
  /** Re-run the loop whenever any of these change (e.g. the fetcher identity). */
  deps: DependencyList;
  /** Called with each fetch error, so a caller can react to e.g. a 401. */
  onError?: (error: unknown) => void;
}

/**
 * Poll `fetcher` on an adaptive interval. Fast while `isActive(data)` holds,
 * idle otherwise. Pauses while the tab is hidden and refreshes immediately when
 * it becomes visible.
 *
 * The whole loop lives inside one effect that owns a single in-flight cycle:
 *  - a generation counter (`gen`) invalidates a superseded effect run, so a
 *    slow response from an old fetcher can't clobber current data (stale-write
 *    guard) and an obsolete cycle can't reschedule;
 *  - `inFlight` prevents a second `run()` (from visibility change or refresh)
 *    starting while one is already awaiting a fetch, so timers never multiply;
 *  - `refresh()` re-enters the *scheduling* loop (not a bare fetch), so manual
 *    refresh keeps auto-polling alive.
 */
export function usePolling<T>(fetcher: () => Promise<T>, opts: PollOptions<T>): PollState<T> {
  const { isActive, deps, onError } = opts;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Latest fetcher/callbacks held in refs so the loop always calls the current
  // one without being torn down and rebuilt on every render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const isActiveRef = useRef(isActive);
  isActiveRef.current = isActive;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  // A trigger that re-arms the loop from outside the effect (used by refresh).
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    let generation = 0; // bumped by refresh to pre-empt an in-flight sleep
    let cancelled = false;
    let inFlight = false;
    let timer: number | undefined;

    const schedule = (active: boolean) => {
      if (cancelled) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(run, active ? FAST_MS : IDLE_MS);
    };

    const run = async () => {
      if (cancelled || inFlight) return; // one cycle at a time
      if (document.hidden) {
        schedule(false); // don't poll a hidden tab; re-check on the idle beat
        return;
      }
      inFlight = true;
      const myGen = generation;
      try {
        const result = await fetcherRef.current();
        if (cancelled || myGen !== generation) return; // superseded → drop it
        setData(result);
        setError(null);
        schedule(isActiveRef.current(result));
      } catch (e) {
        if (cancelled || myGen !== generation) return;
        setError((e as Error).message);
        onErrorRef.current?.(e);
        schedule(false); // on error, back off rather than hammer
      } finally {
        inFlight = false;
        setLoading(false);
      }
    };

    const kick = () => {
      // Pre-empt any in-flight cycle (its late response is dropped via the
      // generation check) and fetch now.
      generation += 1;
      inFlight = false;
      window.clearTimeout(timer);
      timer = window.setTimeout(run, 0);
    };

    const onVisible = () => {
      if (!document.hidden) kick();
    };
    document.addEventListener("visibilitychange", onVisible);

    setLoading(true);
    run();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // refreshNonce is intentionally a dep: bumping it tears down and restarts
    // the loop, which is exactly the immediate-refresh semantics we want.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, refreshNonce]);

  const refresh = useCallback(() => setRefreshNonce((n) => n + 1), []);

  return { data, error, loading, refresh };
}

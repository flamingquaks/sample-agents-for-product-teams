// Small hooks shared by the views: an API client bound to the current auth
// token, and an adaptive polling loop.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "react-oidc-context";
import { DashboardApi } from "./api";
import type { AppConfig } from "./config";

/**
 * A DashboardApi bound to the live Cognito access token. Memoized on the token
 * so a token refresh produces a client that sends the new token, but re-renders
 * don't churn the instance.
 */
export function useApi(config: AppConfig): DashboardApi {
  const auth = useAuth();
  const token = auth.user?.access_token ?? null;
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
  /** Manually trigger an immediate refresh. */
  refresh: () => void;
}

/**
 * Poll `fetcher` on an adaptive interval. `activeSelector` inspects the latest
 * data to decide whether anything is still in flight; if so we poll fast,
 * otherwise we idle. Pauses while the tab is hidden to avoid pointless calls,
 * and refreshes immediately when it becomes visible again.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  activeSelector: (data: T) => boolean,
  deps: unknown[],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const timer = useRef<number | undefined>(undefined);
  // Keep the latest fetcher without making it a poll trigger — deps drive that.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const tick = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      setData(result);
      setError(null);
      return activeSelector(result);
    } catch (e) {
      setError((e as Error).message);
      return false; // on error, back off rather than hammer
    } finally {
      setLoading(false);
    }
    // activeSelector is stable enough for our use; deps below govern restarts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    let cancelled = false;

    const schedule = (active: boolean) => {
      if (cancelled) return;
      const delay = active ? FAST_MS : IDLE_MS;
      timer.current = window.setTimeout(run, delay);
    };
    const run = async () => {
      if (document.hidden) {
        // Don't poll a background tab; re-check shortly.
        schedule(false);
        return;
      }
      const active = await tick();
      schedule(active);
    };

    setLoading(true);
    run();

    const onVisible = () => {
      if (!document.hidden) {
        window.clearTimeout(timer.current);
        run();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      cancelled = true;
      window.clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [tick]);

  const refresh = useCallback(() => {
    window.clearTimeout(timer.current);
    void tick();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  return { data, error, loading, refresh };
}

import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** One loading/error/data machine for every panel, so a 403 from any endpoint
 *  lands in the same place and gets the same treatment. `enabled` defers a request
 *  until there is something to ask for (a session id, say) without a second
 *  code path for "not yet". */
export function useAsync<T>(load: () => Promise<T>, enabled = true): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let live = true;
    setLoading(true);
    setError(null);
    load().then(
      (value) => {
        if (!live) return;
        setData(value);
        setLoading(false);
      },
      (cause) => {
        if (!live) return;
        setError(cause);
        setLoading(false);
      },
    );
    return () => {
      live = false;
    };
  }, [load, enabled, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

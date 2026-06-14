import { useEffect, useRef, useState, useCallback } from "react";

// Poll an async function on an interval and expose the latest value plus
// any error. Skips overlapping calls and keeps the last good value while a
// new request is in flight so the UI never flickers to empty.
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const tick = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const v = await fnRef.current();
      setData(v);
      setError(null);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      busy.current = false;
    }
  }, []);

  useEffect(() => {
    tick();
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [tick, intervalMs]);

  return { data, error, refresh: tick };
}

export function fmtTime(t: number | undefined): string {
  const v = Math.max(0, Math.floor(t || 0));
  const m = Math.floor(v / 60);
  const s = v % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

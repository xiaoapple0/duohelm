import { useEffect, useRef } from "react";

/** active 为 true 时每 intervalMs 调用一次 callback；上一次还没返回时跳过这一轮。 */
export function usePolling(
  callback: () => Promise<void>,
  intervalMs: number,
  active: boolean,
): void {
  const latest = useRef(callback);
  latest.current = callback;

  useEffect(() => {
    if (!active) {
      return;
    }
    let busy = false;
    const timer = window.setInterval(() => {
      if (busy) {
        return;
      }
      busy = true;
      latest.current().finally(() => {
        busy = false;
      });
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [active, intervalMs]);
}

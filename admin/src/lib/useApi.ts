import { useCallback, useEffect, useState } from "react";

import { requestJson } from "@/lib/api";

export function useApi<T>(url: string, refreshKey = 0) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    requestJson<T>(url)
      .then((payload) => {
        if (!active) return;
        setData(payload);
        setError("");
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : "加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [url, refreshKey, nonce]);

  return { data, loading, error, reload };
}

import { useState } from "react";

import { ErrorBanner, LoadingHint, PillTabs } from "@/components/common/Bits";
import UsagePanel from "@/components/UsagePanel";
import type { UsageData } from "@/lib/analytics";
import { conversationLabel } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

export default function UsagePage({ refreshKey }: { refreshKey: number }) {
  const [days, setDays] = useState(7);
  const [conversation, setConversation] = useState("");

  const params = new URLSearchParams({ days: String(days) });
  if (conversation) params.set("conversation", conversation);
  const { data, loading, error } = useApi<UsageData>(
    `/api/usage?${params.toString()}`,
    refreshKey
  );

  const options = data?.conversations ?? [];
  const known = options.some((item) => item.id === conversation);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={conversation}
            onChange={(event) => setConversation(event.target.value)}
            className="h-9 rounded-lg border border-blue-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">全部会话</option>
            {options.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}（{item.total}）
              </option>
            ))}
            {conversation && !known ? (
              <option value={conversation}>{conversationLabel(conversation)}</option>
            ) : null}
          </select>
          <PillTabs
            value={days}
            options={[
              { value: 7, label: "7 天" },
              { value: 30, label: "30 天" },
              { value: 90, label: "90 天" },
            ]}
            onChange={setDays}
          />
        </div>
        <LoadingHint loading={loading} />
      </div>

      <UsagePanel data={data} loading={loading} />
    </div>
  );
}

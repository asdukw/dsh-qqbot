import { useState } from "react";
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Database,
  Gauge,
  Zap,
} from "lucide-react";

import { LoadingHint, PageStack, PillTabs, StatCard } from "@/components/common/Bits";
import UsagePanel from "@/components/UsagePanel";
import { Select } from "@/components/ui/select";
import type { UsageData } from "@/lib/analytics";
import { conversationLabel, formatNumber, formatPercent } from "@/lib/format";
import { useApi } from "@/lib/useApi";

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
  const totals = data?.totals;

  const filters = (
    <>
      <label htmlFor="usage-conversation" className="text-xs text-slate-500">
        会话
      </label>
      <Select
        id="usage-conversation"
        value={conversation}
        onChange={(event) => setConversation(event.target.value)}
        className="w-56"
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
      </Select>
      <PillTabs
        value={days}
        options={[
          { value: 7, label: "7 天" },
          { value: 30, label: "30 天" },
          { value: 90, label: "90 天" },
        ]}
        onChange={setDays}
        label="统计范围"
      />
      <LoadingHint loading={loading && !data} />
    </>
  );

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard
          icon={Database}
          label="总 tokens"
          value={formatNumber(totals?.total)}
          hint={`${formatNumber(totals?.events)} 次模型调用`}
        />
        <StatCard
          icon={ArrowDownToLine}
          label="非缓存输入"
          value={formatNumber(totals?.input)}
          hint="未命中缓存"
        />
        <StatCard
          icon={Zap}
          label="缓存输入"
          value={formatNumber(totals?.cache_read)}
          hint="命中缓存"
        />
        <StatCard
          icon={ArrowUpFromLine}
          label="输出"
          value={formatNumber(totals?.output)}
          hint="生成 token"
        />
        <StatCard
          icon={Gauge}
          label="缓存命中率"
          value={formatPercent(totals?.cache_hit_rate)}
          hint="缓存 / 全部输入"
        />
      </div>

      <UsagePanel data={data} filters={filters} />
    </PageStack>
  );
}

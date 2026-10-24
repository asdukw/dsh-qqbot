import { useState } from "react";
import { MessageSquareText, MessagesSquare, Sparkles, Trophy } from "lucide-react";

import {
  EmptyState,
  ErrorBanner,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import FunPanel from "@/components/FunPanel";
import { Badge } from "@/components/ui/badge";
import type { FunData } from "@/lib/analytics";
import { conversationLabel, formatNumber } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

export default function InsightsPage({ refreshKey }: { refreshKey: number }) {
  const [conversation, setConversation] = useState("");
  const query = conversation ? `?conversation=${encodeURIComponent(conversation)}` : "";
  const { data, loading, error } = useApi<FunData>(`/api/fun${query}`, refreshKey);

  const options = data?.conversations ?? [];
  const known = options.some((item) => item.id === conversation);
  const distribution = options.slice(0, 8);
  const maxMessages = Math.max(1, ...distribution.map((item) => item.messages));

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-slate-500">统计范围</span>
          <select
            value={conversation}
            onChange={(event) => setConversation(event.target.value)}
            className="h-9 rounded-lg border border-blue-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">全部会话</option>
            {options.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}（{item.messages}）
              </option>
            ))}
            {conversation && !known ? (
              <option value={conversation}>{conversationLabel(conversation)}</option>
            ) : null}
          </select>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <Badge variant="outline">{data?.date ?? "今天"}</Badge>
          {loading ? <span>加载中…</span> : null}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={MessageSquareText}
          label="今日消息"
          value={formatNumber(data?.messages)}
          hint="当前筛选范围"
        />
        <StatCard
          icon={Trophy}
          label="排行人数"
          value={formatNumber(data?.ranking.length)}
          hint="发言排行取前 10 名"
        />
        <StatCard
          icon={MessagesSquare}
          label="今日会话"
          value={formatNumber(options.length)}
          hint="产生过消息的群 / 好友"
        />
        <StatCard
          icon={Sparkles}
          label="热词"
          value={formatNumber(data?.words.length)}
          hint="词云候选词数量"
        />
      </div>

      <SectionCard
        icon={MessagesSquare}
        title="今日会话分布"
        description="按入站消息数排序，取前 8 个会话"
        badge={distribution.length}
      >
        {distribution.length ? (
          <div className="divide-y divide-blue-50">
            {distribution.map((item) => (
              <div key={item.id} className="px-5 py-3">
                <div className="flex items-center justify-between gap-3 text-sm">
                  <button
                    type="button"
                    onClick={() => setConversation(item.id)}
                    className={cn(
                      "truncate text-left font-medium",
                      item.id === conversation ? "text-blue-700" : "text-slate-700 hover:text-blue-700"
                    )}
                  >
                    {item.label}
                  </button>
                  <span className="shrink-0 text-xs tabular-nums text-slate-500">
                    {formatNumber(item.messages)} 条
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-blue-50">
                  <div
                    className="h-full rounded-full bg-blue-500"
                    style={{ width: `${Math.max(3, (item.messages / maxMessages) * 100)}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon={MessagesSquare} text="今天还没有会话消息。" />
        )}
      </SectionCard>

      <FunPanel data={data} loading={loading} />
    </div>
  );
}

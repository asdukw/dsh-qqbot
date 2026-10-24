import { useState } from "react";
import { MessageSquareText, MessagesSquare, Sparkles, Trophy } from "lucide-react";

import {
  EmptyState,
  ErrorBanner,
  PillTabs,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import FunPanel from "@/components/FunPanel";
import { Badge } from "@/components/ui/badge";
import type { FunData } from "@/lib/analytics";
import { conversationLabel, formatNumber } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type QuickRange = "today" | "week" | "month" | "custom";

const QUICK_OPTIONS: Array<{ value: QuickRange; label: string }> = [
  { value: "today", label: "今天" },
  { value: "week", label: "近一周" },
  { value: "month", label: "近一月" },
];

const DATE_INPUT =
  "h-9 rounded-lg border border-blue-200 bg-white px-2.5 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-blue-500";

function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shanghaiToday(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const value = (type: string) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function shiftedDate(base: string, days: number): string {
  const [year, month, day] = base.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  date.setDate(date.getDate() + days);
  return isoDate(date);
}

function rangeLabel(data: FunData | null): string {
  if (!data) return "加载中…";
  return data.start === data.end ? data.start : `${data.start} ~ ${data.end}`;
}

export default function InsightsPage({ refreshKey }: { refreshKey: number }) {
  const today = shanghaiToday();
  const [start, setStart] = useState(today);
  const [end, setEnd] = useState(today);
  const [quick, setQuick] = useState<QuickRange>("today");
  const [conversation, setConversation] = useState("");

  const params = new URLSearchParams({ start, end });
  if (conversation) params.set("conversation", conversation);
  const { data, loading, error } = useApi<FunData>(
    `/api/fun?${params.toString()}`,
    refreshKey
  );

  const options = data?.conversations ?? [];
  const known = options.some((item) => item.id === conversation);
  const distribution = options.slice(0, 8);
  const maxMessages = Math.max(1, ...distribution.map((item) => item.messages));

  function applyQuick(value: QuickRange) {
    if (value === "custom") return;
    const span = value === "today" ? 1 : value === "week" ? 7 : 30;
    setStart(shiftedDate(today, -(span - 1)));
    setEnd(today);
    setQuick(value);
  }

  function changeStart(value: string) {
    if (!value) return;
    const next = value > today ? today : value;
    setStart(next);
    setEnd((current) => {
      const clamped = current > today ? today : current;
      return clamped < next ? next : clamped;
    });
    setQuick("custom");
  }

  function changeEnd(value: string) {
    if (!value) return;
    const next = value > today ? today : value;
    setEnd(next);
    setStart((current) => (current > next ? next : current));
    setQuick("custom");
  }

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-slate-500">时间范围</span>
          <PillTabs value={quick} options={QUICK_OPTIONS} onChange={applyQuick} />
          <div className="flex items-center gap-1.5">
            <input
              type="date"
              value={start}
              max={end}
              onChange={(event) => changeStart(event.target.value)}
              className={DATE_INPUT}
              aria-label="起始日期"
            />
            <span className="text-xs text-slate-400">至</span>
            <input
              type="date"
              value={end}
              min={start}
              max={today}
              onChange={(event) => changeEnd(event.target.value)}
              className={DATE_INPUT}
              aria-label="终止日期"
            />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-slate-500">会话</span>
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
          {loading ? <span className="text-xs text-slate-500">加载中…</span> : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <Badge variant="outline">{rangeLabel(data)}</Badge>
        {data && data.days > 1 ? <span>{data.days} 天</span> : null}
        <span>·</span>
        <span>排行取前 10 名，词云取范围内最近 2000 条消息</span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={MessageSquareText}
          label="消息总数"
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
          label="会话数"
          value={formatNumber(data?.conversation_total)}
          hint="范围内产生过消息的群 / 好友"
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
        title="会话分布"
        description="按入站消息数排序，取前 8 个会话"
        badge={distribution.length}
      >
        {loading && !data ? (
          <div className="px-5 py-10 text-center text-sm text-slate-400">加载中…</div>
        ) : distribution.length ? (
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
          <EmptyState icon={MessagesSquare} text="这个范围内还没有会话消息。" />
        )}
      </SectionCard>

      <FunPanel data={data} loading={loading} />
    </div>
  );
}

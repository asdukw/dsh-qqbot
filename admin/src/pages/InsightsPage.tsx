import { useState } from "react";
import { MessageSquareText, MessagesSquare, Trophy } from "lucide-react";

import {
  EmptyState,
  LoadingHint,
  PageStack,
  PillTabs,
  SectionCard,
  StatCard,
  Toolbar,
} from "@/components/common/Bits";
import FunPanel from "@/components/FunPanel";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
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
    <PageStack error={error} loading={loading} hasData={!!data}>
      <Toolbar className="justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <PillTabs
            value={quick}
            options={QUICK_OPTIONS}
            onChange={applyQuick}
            label="时间范围"
          />
          <div className="flex items-center gap-1.5">
            <Input
              type="date"
              value={start}
              max={end}
              onChange={(event) => changeStart(event.target.value)}
              className="w-36"
              aria-label="起始日期"
            />
            <span className="text-xs text-slate-400">至</span>
            <Input
              type="date"
              value={end}
              min={start}
              max={today}
              onChange={(event) => changeEnd(event.target.value)}
              className="w-36"
              aria-label="终止日期"
            />
          </div>
          <Select
            value={conversation}
            onChange={(event) => setConversation(event.target.value)}
            className="w-48"
            aria-label="会话筛选"
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
          </Select>
        </div>
        <LoadingHint loading={loading} />
      </Toolbar>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard
          icon={MessageSquareText}
          label="消息总数"
          value={formatNumber(data?.messages)}
        />
        <StatCard
          icon={Trophy}
          label="参与人数"
          value={formatNumber(data?.ranking.length)}
        />
        <StatCard
          icon={MessagesSquare}
          label="会话数"
          value={formatNumber(data?.conversation_total)}
        />
      </div>

      <SectionCard
        icon={MessagesSquare}
        title="会话分布"
        description="消息最多的 8 个会话"
        badge={distribution.length}
      >
        {loading && !data ? (
          <div className="px-5 py-6">
            <LoadingHint loading />
          </div>
        ) : distribution.length ? (
          <div className="divide-y divide-slate-100">
            {distribution.map((item) => (
              <div key={item.id} className="px-5 py-3">
                <div className="flex items-center justify-between gap-3 text-sm">
                  <button
                    type="button"
                    onClick={() => setConversation(item.id)}
                    className={cn(
                      "truncate text-left font-medium",
                      item.id === conversation
                        ? "text-primary"
                        : "text-slate-700 hover:text-primary"
                    )}
                  >
                    {item.label}
                  </button>
                  <span className="shrink-0 text-xs tabular-nums text-slate-500">
                    {formatNumber(item.messages)} 条
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max(3, (item.messages / maxMessages) * 100)}%`,
                      backgroundColor: "var(--chart-1)",
                    }}
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
    </PageStack>
  );
}

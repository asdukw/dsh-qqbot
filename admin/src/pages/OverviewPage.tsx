import { useState } from "react";
import {
  AlertTriangle,
  Bot,
  CalendarClock,
  LayoutDashboard,
  MessageSquare,
  MessagesSquare,
  Rss,
  Zap,
} from "lucide-react";

import {
  EmptyState,
  InfoRow,
  LoadingHint,
  PageStack,
  PillTabs,
  SectionCard,
  SectionHeading,
  StatCard,
} from "@/components/common/Bits";
import TrendChart from "@/components/common/TrendChart";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { useApi } from "@/lib/useApi";
import type { OverviewData } from "@/lib/analytics";
import { formatCompact, formatNumber, formatTs } from "@/lib/format";
import type { RouteId } from "@/lib/router";

function Sparkline({ values }: { values: number[] }) {
  const width = 220;
  const height = 48;
  const pad = 3;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0;
  const points = values.map((value, index) => {
    const x = pad + index * step;
    const y = height - pad - (value / max) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const area = `M${pad},${height - pad} L${points.join(" L")} L${(pad + (values.length - 1) * step).toFixed(1)},${height - pad} Z`;
  const lastX = pad + (values.length - 1) * step;
  const lastY = height - pad - ((values[values.length - 1] ?? 0) / max) * (height - pad * 2);

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-12 w-full" aria-hidden="true">
      <path d={area} fill="var(--chart-2)" fillOpacity={0.1} />
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--chart-2)"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx={lastX} cy={lastY} r={3.5} fill="var(--chart-2)" stroke="#ffffff" strokeWidth={2} />
    </svg>
  );
}

export default function OverviewPage({
  refreshKey,
  onNavigate,
}: {
  refreshKey: number;
  onNavigate: (id: RouteId) => void;
}) {
  const [days, setDays] = useState(7);
  const { data, loading, error } = useApi<OverviewData>(
    `/api/overview?days=${days}`,
    refreshKey
  );

  if (!data) {
    return (
      <PageStack error={error} loading={loading} hasData={!!data}>
        <Card>
          <div className="flex h-40 items-center justify-center">
            {loading ? (
              <LoadingHint loading label="正在加载仪表盘…" />
            ) : (
              <EmptyState icon={LayoutDashboard} text="没有仪表盘数据。" />
            )}
          </div>
        </Card>
      </PageStack>
    );
  }

  const counts = data.counts;
  const today = data.today;
  const previous = data.trend.length > 1 ? data.trend[data.trend.length - 2].messages : 0;
  const delta = today.messages - previous;
  const messageTrend = data.trend.map((point) => ({ day: point.day, value: point.messages }));
  const tokenTrend = data.trend.map((point) => ({ day: point.day, value: point.tokens }));
  const spark = data.trend.slice(-7).map((point) => point.messages);

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card className="overflow-hidden sm:col-span-2">
          <CardContent className="p-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  今日消息
                </p>
                <p className="mt-1 text-5xl font-semibold tracking-tight text-slate-900 tabular-nums">
                  {formatNumber(today.messages)}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  较昨日 {delta >= 0 ? "+" : ""}
                  {formatNumber(delta)} · {formatNumber(today.conversations)} 个会话
                </p>
              </div>
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
                <MessageSquare className="h-5 w-5" />
              </div>
            </div>
            <div className="mt-4">
              <Sparkline values={spark} />
            </div>
          </CardContent>
        </Card>

        <StatCard
          icon={Zap}
          label="今日 Token"
          value={formatCompact(today.tokens)}
          hint={`${formatNumber(today.calls)} 次模型调用`}
        />
        <StatCard
          icon={MessagesSquare}
          label="已知会话"
          value={formatNumber(counts.conversations)}
          hint="群聊 / 好友 / 任务"
          onClick={() => onNavigate("usage")}
        />
        <StatCard
          icon={CalendarClock}
          label="定时任务"
          value={`${counts.tasks.enabled}/${counts.tasks.total}`}
          hint="启用 / 总数"
          onClick={() => onNavigate("tasks")}
        />
        <StatCard
          icon={Rss}
          label="订阅推送"
          value={formatNumber(counts.feeds.total)}
          hint={counts.feeds.errors ? `${counts.feeds.errors} 个异常` : "全部正常"}
          tone={counts.feeds.errors ? "red" : "blue"}
          onClick={() => onNavigate("feeds")}
        />
      </div>

      <section className="space-y-3">
        <SectionHeading
          title="趋势"
          description="按自然日统计"
          actions={
            <PillTabs
              value={days}
              label="统计范围"
              options={[
                { value: 7, label: "7 天" },
                { value: 30, label: "30 天" },
              ]}
              onChange={setDays}
            />
          }
        />
        <div className="grid gap-4 xl:grid-cols-2">
          <TrendChart title="消息量" description="入站消息" data={messageTrend} />
          <TrendChart
            title="Token 用量"
            description="模型调用消耗"
            data={tokenTrend}
            formatValue={formatCompact}
          />
        </div>
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.25fr_1fr]">
        <SectionCard
          icon={AlertTriangle}
          title="需要关注"
          description="订阅拉取失败"
          badge={data.alerts.length}
        >
          {data.alerts.length ? (
            <div className="divide-y divide-slate-100">
              {data.alerts.map((alert) => (
                <button
                  key={`${alert.kind}-${alert.id}`}
                  type="button"
                  onClick={() => onNavigate("feeds")}
                  className="flex w-full items-start justify-between gap-3 px-5 py-4 text-left transition-colors hover:bg-slate-50"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Badge variant="destructive">
                        <AlertTriangle className="mr-1 h-3 w-3" /> 订阅异常
                      </Badge>
                      <span className="truncate text-sm font-medium text-slate-900">
                        {alert.title}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs text-red-600">{alert.detail}</p>
                  </div>
                  <span className="shrink-0 text-xs text-slate-500 tabular-nums">
                    {formatTs(alert.at)}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={AlertTriangle}
              text="暂无异常"
              hint="订阅拉取失败时会显示在这里。"
            />
          )}
        </SectionCard>

        <SectionCard icon={Bot} title="当前配置">
          <div className="divide-y divide-slate-100">
            <InfoRow label="当前模型" value={data.active_model} mono />
            <InfoRow label="当前角色卡" value={data.active_card} />
          </div>
        </SectionCard>
      </div>

      <SectionCard
        icon={MessageSquare}
        title="最近消息"
        description="最新入站消息"
        badge={data.recent_messages.length}
        actions={
          <button
            type="button"
            onClick={() => onNavigate("insights")}
            className="text-xs font-medium text-blue-600 hover:text-blue-700"
          >
            查看聊天分析 →
          </button>
        }
      >
        {data.recent_messages.length ? (
          <div className="divide-y divide-slate-100">
            {data.recent_messages.map((message, index) => (
              <div
                key={`${message.conversation_id}-${message.created_at}-${index}`}
                className="flex items-start gap-3 px-5 py-3"
              >
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-blue-50 text-xs font-semibold text-blue-600">
                  {(message.sender_name || "?").slice(0, 1)}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="truncate text-sm font-medium text-slate-900">
                      {message.sender_name || "未知"}
                    </span>
                    <span className="text-xs text-slate-500">{message.label}</span>
                    <span className="ml-auto text-xs text-slate-500 tabular-nums">
                      {formatTs(message.created_at)}
                    </span>
                  </div>
                  <p className="mt-0.5 line-clamp-1 text-xs text-slate-500" title={message.text}>
                    {message.text || "（无文本）"}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon={MessageSquare} text="暂无消息" />
        )}
      </SectionCard>
    </PageStack>
  );
}

import type { ComponentType } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowUpFromLine,
  Database,
  Gauge,
  Loader2,
  Zap,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  formatNumber,
  formatPercent,
  conversationLabel,
} from "@/lib/format";
import type { UsageData } from "@/lib/analytics";

const PURPOSE_LABELS: Record<string, string> = {
  chat: "聊天",
  proactive: "主动接话",
  compaction: "上下文压缩",
  image_caption: "图片描述",
  sticker_tag: "表情打标",
  task_parse: "任务解析",
  task_generate: "任务文案",
  check: "连通性检查",
};

function purposeLabel(value: string) {
  return PURPOSE_LABELS[value] || value || "其他";
}

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="flex items-start justify-between p-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
          <p className="mt-1 truncate text-xl font-semibold text-slate-900" title={value}>
            {value}
          </p>
          {hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
        </div>
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
          <Icon className="h-4 w-4" />
        </div>
      </CardContent>
    </Card>
  );
}

export default function UsagePanel({
  data,
  loading,
}: {
  data: UsageData | null;
  loading: boolean;
}) {
  const totals = data?.totals;
  const maxDay = Math.max(1, ...(data?.by_day.map((day) => day.total) ?? [1]));
  const maxPurpose = Math.max(1, ...(data?.by_purpose.map((row) => row.total) ?? [1]));

  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-3 space-y-0 border-b border-blue-50 pb-4">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
            <Activity className="h-5 w-5" />
          </div>
          <div>
            <CardTitle>Token 用量</CardTitle>
            <CardDescription className="mt-1">
              主会话与辅助调用都记录；缓存命中单独计价
            </CardDescription>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">{data?.days ?? 7} 天</Badge>
          {loading ? <Loader2 className="h-4 w-4 animate-spin text-blue-600" /> : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-5 p-5">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <MetricCard
            icon={Database}
            label="总 tokens"
            value={formatNumber(totals?.total)}
            hint={`${formatNumber(totals?.events)} 次模型调用`}
          />
          <MetricCard
            icon={ArrowDownToLine}
            label="非缓存输入"
            value={formatNumber(totals?.input)}
            hint="按 miss 价格计费的部分"
          />
          <MetricCard
            icon={Zap}
            label="缓存输入"
            value={formatNumber(totals?.cache_read)}
            hint="按 cache-hit 价格计费"
          />
          <MetricCard
            icon={ArrowUpFromLine}
            label="输出"
            value={formatNumber(totals?.output)}
            hint="生成 token"
          />
          <MetricCard
            icon={Gauge}
            label="缓存命中率"
            value={formatPercent(totals?.cache_hit_rate)}
            hint="缓存 / 全部输入"
          />
        </div>

        <div className="grid gap-5 xl:grid-cols-2">
          <div className="rounded-xl border border-blue-100">
            <div className="flex items-center justify-between border-b border-blue-50 px-4 py-3">
              <h4 className="text-sm font-semibold text-slate-800">每日用量</h4>
              <Badge variant="outline">{data?.days ?? 7} 天</Badge>
            </div>
            {data?.by_day.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>日期</TableHead>
                    <TableHead>输入</TableHead>
                    <TableHead>缓存</TableHead>
                    <TableHead>输出</TableHead>
                    <TableHead className="w-[35%]">总量</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.by_day.map((day) => (
                    <TableRow key={day.day}>
                      <TableCell className="whitespace-nowrap text-xs">{day.day}</TableCell>
                      <TableCell className="text-xs">{formatNumber(day.input)}</TableCell>
                      <TableCell className="text-xs">{formatNumber(day.cache_read)}</TableCell>
                      <TableCell className="text-xs">{formatNumber(day.output)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 min-w-10 flex-1 overflow-hidden rounded-full bg-blue-50">
                            <div
                              className="h-full rounded-full bg-blue-500"
                              style={{ width: `${Math.max(2, (day.total / maxDay) * 100)}%` }}
                            />
                          </div>
                          <span className="w-16 text-right text-xs text-slate-500">
                            {formatNumber(day.total)}
                          </span>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="px-4 py-8 text-center text-sm text-muted-foreground">还没有用量记录。</p>
            )}
          </div>

          <div className="rounded-xl border border-blue-100">
            <div className="flex items-center justify-between border-b border-blue-50 px-4 py-3">
              <h4 className="text-sm font-semibold text-slate-800">按用途</h4>
              <Badge variant="outline">{data?.by_purpose.length ?? 0} 类</Badge>
            </div>
            {data?.by_purpose.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>用途</TableHead>
                    <TableHead>输入</TableHead>
                    <TableHead>缓存</TableHead>
                    <TableHead>输出</TableHead>
                    <TableHead className="w-[32%]">占比</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.by_purpose.map((row) => (
                    <TableRow key={row.purpose}>
                      <TableCell className="whitespace-nowrap text-xs font-medium">
                        {purposeLabel(row.purpose)}
                      </TableCell>
                      <TableCell className="text-xs">{formatNumber(row.input)}</TableCell>
                      <TableCell className="text-xs">{formatNumber(row.cache_read)}</TableCell>
                      <TableCell className="text-xs">{formatNumber(row.output)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 min-w-10 flex-1 overflow-hidden rounded-full bg-blue-50">
                            <div
                              className="h-full rounded-full bg-sky-500"
                              style={{ width: `${Math.max(2, (row.total / maxPurpose) * 100)}%` }}
                            />
                          </div>
                          <span className="w-16 text-right text-xs text-slate-500">
                            {formatNumber(row.total)}
                          </span>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="px-4 py-8 text-center text-sm text-muted-foreground">还没有用量记录。</p>
            )}
          </div>
        </div>

        <div className="rounded-xl border border-blue-100">
          <div className="flex items-center justify-between border-b border-blue-50 px-4 py-3">
            <h4 className="text-sm font-semibold text-slate-800">会话排行</h4>
            <Badge variant="outline">Top {data?.by_session.length ?? 0}</Badge>
          </div>
          {data?.by_session.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>会话</TableHead>
                  <TableHead>Session</TableHead>
                  <TableHead>输入</TableHead>
                  <TableHead>缓存</TableHead>
                  <TableHead>输出</TableHead>
                  <TableHead>总 tokens</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.by_session.map((row) => (
                  <TableRow key={`${row.conversation_id}/${row.session_id}`}>
                    <TableCell className="whitespace-nowrap text-xs">
                      {conversationLabel(row.conversation_id)}
                    </TableCell>
                    <TableCell className="text-xs text-slate-500">
                      {row.session_id || "-"}
                    </TableCell>
                    <TableCell className="text-xs">{formatNumber(row.input)}</TableCell>
                    <TableCell className="text-xs">{formatNumber(row.cache_read)}</TableCell>
                    <TableCell className="text-xs">{formatNumber(row.output)}</TableCell>
                    <TableCell className="text-xs font-medium">{formatNumber(row.total)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="px-4 py-8 text-center text-sm text-muted-foreground">还没有会话用量。</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

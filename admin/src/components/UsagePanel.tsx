import type { ReactNode } from "react";
import { Activity, Database } from "lucide-react";

import { EmptyState, FilterBar, SectionCard } from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatNumber, conversationLabel } from "@/lib/format";
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

/** 卡片内的次级面板，与 Card 区分层级。 */
function SubPanel({
  title,
  badge,
  children,
}: {
  title: string;
  badge?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200">
      <div className="flex items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
        <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
        {badge}
      </div>
      {children}
    </div>
  );
}

export default function UsagePanel({
  data,
  filters,
}: {
  data: UsageData | null;
  filters?: ReactNode;
}) {
  const maxDay = Math.max(1, ...(data?.by_day.map((day) => day.total) ?? [1]));
  const maxPurpose = Math.max(1, ...(data?.by_purpose.map((row) => row.total) ?? [1]));

  return (
    <SectionCard
      icon={Activity}
      title="用量明细"
      description="按日、用途与会话拆分；缓存命中单独统计"
    >
      {filters ? <FilterBar>{filters}</FilterBar> : null}
      <div className="space-y-5 p-5">
        <div className="grid gap-5 xl:grid-cols-2">
          <SubPanel title="每日用量">
            {data?.by_day.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>日期</TableHead>
                    <TableHead className="text-right">输入</TableHead>
                    <TableHead className="text-right">缓存</TableHead>
                    <TableHead className="text-right">输出</TableHead>
                    <TableHead className="w-[35%] text-right">总量</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.by_day.map((day) => (
                    <TableRow key={day.day}>
                      <TableCell className="whitespace-nowrap text-xs tabular-nums">
                        {day.day}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatNumber(day.input)}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatNumber(day.cache_read)}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatNumber(day.output)}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 min-w-10 flex-1 overflow-hidden rounded-full bg-slate-100">
                            <div
                              className="h-full rounded-full bg-[var(--chart-2)]"
                              style={{ width: `${Math.max(2, (day.total / maxDay) * 100)}%` }}
                            />
                          </div>
                          <span className="w-16 text-right text-xs tabular-nums text-slate-500">
                            {formatNumber(day.total)}
                          </span>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <EmptyState icon={Database} text="还没有用量记录" />
            )}
          </SubPanel>

          <SubPanel
            title="按用途"
            badge={<Badge variant="outline">{data?.by_purpose.length ?? 0} 类</Badge>}
          >
            {data?.by_purpose.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>用途</TableHead>
                    <TableHead className="text-right">输入</TableHead>
                    <TableHead className="text-right">缓存</TableHead>
                    <TableHead className="text-right">输出</TableHead>
                    <TableHead className="w-[32%] text-right">占比</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.by_purpose.map((row) => (
                    <TableRow key={row.purpose}>
                      <TableCell className="whitespace-nowrap text-xs font-medium">
                        {purposeLabel(row.purpose)}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatNumber(row.input)}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatNumber(row.cache_read)}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatNumber(row.output)}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 min-w-10 flex-1 overflow-hidden rounded-full bg-slate-100">
                            <div
                              className="h-full rounded-full bg-[var(--chart-2)]"
                              style={{ width: `${Math.max(2, (row.total / maxPurpose) * 100)}%` }}
                            />
                          </div>
                          <span className="w-16 text-right text-xs tabular-nums text-slate-500">
                            {formatNumber(row.total)}
                          </span>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <EmptyState icon={Activity} text="还没有用量记录" />
            )}
          </SubPanel>
        </div>

        <SubPanel
          title="会话排行"
          badge={<Badge variant="outline">Top {data?.by_session.length ?? 0}</Badge>}
        >
          {data?.by_session.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>会话</TableHead>
                  <TableHead className="text-right">输入</TableHead>
                  <TableHead className="text-right">缓存</TableHead>
                  <TableHead className="text-right">输出</TableHead>
                  <TableHead className="text-right">总 tokens</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.by_session.map((row) => (
                  <TableRow key={`${row.conversation_id}/${row.session_id}`}>
                    <TableCell
                      className="whitespace-nowrap text-xs"
                      title={row.session_id ? `session ${row.session_id}` : undefined}
                    >
                      {conversationLabel(row.conversation_id)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {formatNumber(row.input)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {formatNumber(row.cache_read)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {formatNumber(row.output)}
                    </TableCell>
                    <TableCell className="text-right text-xs font-medium tabular-nums">
                      {formatNumber(row.total)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <EmptyState icon={Activity} text="还没有会话用量" />
          )}
        </SubPanel>
      </div>
    </SectionCard>
  );
}

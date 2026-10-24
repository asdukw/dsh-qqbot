import { useMemo, useState } from "react";
import {
  CalendarClock,
  CheckCircle2,
  Clock,
  PauseCircle,
  Play,
} from "lucide-react";

import {
  EmptyState,
  ErrorBanner,
  PillTabs,
  SearchField,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { TasksData } from "@/lib/analytics";
import {
  formatNumber,
  formatTs,
  scheduleText,
  targetLabel,
  taskModeLabel,
} from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type StatusFilter = "all" | "enabled" | "disabled";

export default function TasksPage({ refreshKey }: { refreshKey: number }) {
  const [status, setStatus] = useState<StatusFilter>("all");
  const [search, setSearch] = useState("");
  const { data, loading, error } = useApi<TasksData>("/api/tasks", refreshKey);

  const rows = useMemo(() => {
    const all = data?.tasks ?? [];
    const query = search.trim().toLowerCase();
    return all.filter((task) => {
      if (status === "enabled" && !task.enabled) return false;
      if (status === "disabled" && task.enabled) return false;
      if (!query) return true;
      return [
        String(task.id),
        task.message,
        task.instruction,
        `${task.chat_type}:${task.chat_id}`,
      ].some((value) => value.toLowerCase().includes(query));
    });
  }, [data, status, search]);

  const all = data?.tasks ?? [];
  const enabled = all.filter((task) => task.enabled).length;
  const runs = all.reduce((sum, task) => sum + task.runs, 0);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={CalendarClock} label="任务总数" value={formatNumber(all.length)} hint="按群 / 私聊绑定" />
        <StatCard icon={Play} label="运行中" value={formatNumber(enabled)} hint="调度器会继续触发" tone="emerald" />
        <StatCard
          icon={PauseCircle}
          label="已暂停"
          value={formatNumber(all.length - enabled)}
          hint="保留配置但不触发"
          tone="amber"
        />
        <StatCard icon={CheckCircle2} label="累计执行" value={formatNumber(runs)} hint="所有任务的 runs 之和" />
      </div>

      <SectionCard
        icon={CalendarClock}
        title="任务列表"
        description="定时任务由模型解析或 /task 命令创建"
        badge={rows.length}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-blue-50 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <PillTabs
              value={status}
              options={[
                { value: "all", label: "全部" },
                { value: "enabled", label: "运行中" },
                { value: "disabled", label: "已暂停" },
              ]}
              onChange={setStatus}
            />
            <SearchField
              value={search}
              onChange={setSearch}
              placeholder="搜索内容、指令或目标…"
              className="w-64"
            />
          </div>
          {loading ? <span className="text-xs text-slate-500">加载中…</span> : null}
        </div>

        {rows.length ? (
          <Table className="min-w-[980px]">
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>排期</TableHead>
                <TableHead>目标</TableHead>
                <TableHead>下次运行</TableHead>
                <TableHead>上次运行</TableHead>
                <TableHead className="text-right">已运行</TableHead>
                <TableHead>模式</TableHead>
                <TableHead>内容</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((task) => (
                <TableRow key={task.id}>
                  <TableCell className="font-mono text-xs text-slate-500">#{task.id}</TableCell>
                  <TableCell>
                    {task.enabled ? (
                      <Badge variant="success">
                        <CheckCircle2 className="mr-1 h-3 w-3" /> 运行中
                      </Badge>
                    ) : (
                      <Badge variant="muted">
                        <PauseCircle className="mr-1 h-3 w-3" /> 已暂停
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-sm">{scheduleText(task)}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {targetLabel(task.chat_type, task.chat_id)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {task.enabled ? formatTs(task.next_run) : "-"}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {formatTs(task.last_run)}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {formatNumber(task.runs)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    <Badge variant="outline">{taskModeLabel(task.mode)}</Badge>
                  </TableCell>
                  <TableCell className="max-w-[20rem]">
                    <div className="line-clamp-2 text-sm text-slate-800" title={task.message}>
                      {task.message}
                    </div>
                    {task.instruction && task.instruction !== task.message ? (
                      <div className="mt-0.5 line-clamp-1 text-[11px] text-slate-400" title={task.instruction}>
                        原始指令：{task.instruction}
                      </div>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <EmptyState
            icon={Clock}
            text="没有匹配的定时任务。"
            hint="管理员在聊天里说“每天早上 8 点提醒大家喝水”即可创建。"
          />
        )}
      </SectionCard>
    </div>
  );
}

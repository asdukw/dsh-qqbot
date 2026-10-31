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
  FilterBar,
  LoadingHint,
  PageStack,
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
    <PageStack error={error} loading={loading} hasData={!!data}>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={CalendarClock} label="任务总数" value={formatNumber(all.length)} />
        <StatCard icon={Play} label="运行中" value={formatNumber(enabled)} hint="持续触发" tone="emerald" />
        <StatCard
          icon={PauseCircle}
          label="已暂停"
          value={formatNumber(all.length - enabled)}
          hint="不会触发"
          tone="amber"
        />
        <StatCard icon={CheckCircle2} label="累计执行" value={formatNumber(runs)} />
      </div>

      <SectionCard
        icon={CalendarClock}
        title="任务列表"
        description="由模型解析或 /task 命令创建"
        badge={rows.length}
      >
        <FilterBar>
          <PillTabs
            value={status}
            options={[
              { value: "all", label: "全部" },
              { value: "enabled", label: "运行中" },
              { value: "disabled", label: "已暂停" },
            ]}
            onChange={setStatus}
            label="任务状态筛选"
          />
          <SearchField
            value={search}
            onChange={setSearch}
            placeholder="搜索内容、指令或目标…"
            className="w-64"
          />
        </FilterBar>

        {loading && !data ? (
          <div className="flex justify-center px-5 py-10">
            <LoadingHint loading />
          </div>
        ) : rows.length ? (
          <Table className="min-w-[900px]">
            <TableHeader>
              <TableRow>
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
                  <TableCell>
                    <div className="flex items-center gap-2">
                      {task.enabled ? (
                        <Badge variant="success">
                          <CheckCircle2 className="mr-1 h-3 w-3" /> 运行中
                        </Badge>
                      ) : (
                        <Badge variant="muted">
                          <PauseCircle className="mr-1 h-3 w-3" /> 已暂停
                        </Badge>
                      )}
                      <span className="font-mono text-[11px] text-slate-400">#{task.id}</span>
                    </div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-sm">{scheduleText(task)}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {targetLabel(task.chat_type, task.chat_id)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500 tabular-nums">
                    {task.enabled ? formatTs(task.next_run) : "-"}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500 tabular-nums">
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
            hint="在聊天里用自然语言或 /task 命令创建。"
          />
        )}
      </SectionCard>
    </PageStack>
  );
}

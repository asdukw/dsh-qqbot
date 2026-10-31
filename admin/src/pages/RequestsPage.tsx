import { useEffect, useMemo, useState } from "react";
import {
  Check,
  Clock,
  Inbox,
  UserCheck,
  UserPlus,
  Users,
  X,
} from "lucide-react";

import {
  EmptyState,
  FilterBar,
  LoadingHint,
  Notice,
  PageStack,
  PillTabs,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { ApprovalRow, ApprovalsData } from "@/lib/analytics";
import { requestJson } from "@/lib/api";
import { formatNumber, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Filter = "active" | "all";

type Result = { kind: "ok" | "error"; text: string } | null;

const KIND_LABEL: Record<ApprovalRow["kind"], string> = {
  friend: "好友请求",
  group_invite: "群邀请",
  group_add: "入群申请",
};

const KIND_ICON: Record<ApprovalRow["kind"], typeof UserPlus> = {
  friend: UserPlus,
  group_invite: Users,
  group_add: UserCheck,
};

const STATUS_LABEL: Record<ApprovalRow["status"], string> = {
  pending: "待处理",
  queued: "已提交",
  executing: "执行中",
  approved: "已同意",
  rejected: "已拒绝",
  failed: "失败",
};

function statusVariant(status: ApprovalRow["status"]) {
  if (status === "approved") return "success" as const;
  if (status === "failed") return "destructive" as const;
  if (status === "rejected") return "muted" as const;
  if (status === "pending") return "secondary" as const;
  return "outline" as const;
}

function isActive(row: ApprovalRow) {
  return row.status === "pending" || row.status === "queued" || row.status === "executing";
}

function requesterLabel(row: ApprovalRow) {
  if (row.requester_name) return `${row.requester_name}（${row.user_id}）`;
  return `QQ ${row.user_id}`;
}

function groupLabel(row: ApprovalRow) {
  if (!row.group_id) return "-";
  if (row.group_name) return `${row.group_name}（${row.group_id}）`;
  return `群 ${row.group_id}`;
}

function shanghaiTodayStart(): number {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const value = (type: string) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return Math.floor(
    new Date(
      `${value("year")}-${value("month")}-${value("day")}T00:00:00+08:00`
    ).getTime() / 1000
  );
}

export default function RequestsPage({ refreshKey }: { refreshKey: number }) {
  const { data, loading, error, reload } = useApi<ApprovalsData>(
    "/api/requests",
    refreshKey
  );
  const [filter, setFilter] = useState<Filter>("active");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notice, setNotice] = useState<Result>(null);

  const all = data?.requests ?? [];
  const active = useMemo(() => all.filter(isActive), [all]);
  const rows = filter === "active" ? active : all;
  const todayStart = useMemo(shanghaiTodayStart, []);
  const todayCount = all.filter((row) => row.created_at >= todayStart).length;
  const approved = all.filter((row) => row.status === "approved").length;
  const rejected = all.filter((row) => row.status === "rejected").length;

  const hasQueued = all.some(
    (row) => row.status === "queued" || row.status === "executing"
  );

  useEffect(() => {
    if (!hasQueued) return;
    const timer = window.setInterval(reload, 3000);
    return () => window.clearInterval(timer);
  }, [hasQueued, reload]);

  async function decide(row: ApprovalRow, action: "approve" | "reject") {
    setBusyId(row.id);
    setNotice(null);
    try {
      await requestJson<ApprovalRow>("/api/requests/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: row.id, action }),
      });
      setNotice({
        kind: "ok",
        text: action === "approve" ? "已提交同意，bot 正在执行。" : "已提交拒绝，bot 正在执行。",
      });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: err instanceof Error ? err.message : "提交失败",
      });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      {data && !data.enabled ? (
        <Notice tone="warning">
          审批功能未启用，收到的新请求不会入库。
        </Notice>
      ) : null}
      {notice ? (
        <Notice tone={notice.kind === "ok" ? "success" : "danger"}>{notice.text}</Notice>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Inbox}
          label="待处理"
          value={formatNumber(active.length)}
          tone={active.length ? "amber" : "blue"}
        />
        <StatCard icon={Clock} label="今日新增" value={formatNumber(todayCount)} hint="北京时间自然日" />
        <StatCard icon={Check} label="已同意" value={formatNumber(approved)} tone="emerald" />
        <StatCard icon={X} label="已拒绝" value={formatNumber(rejected)} tone="slate" />
      </div>

      <SectionCard
        icon={UserCheck}
        title="请求列表"
        description="同意或拒绝后由 bot 在几秒内执行"
        badge={rows.length}
      >
        <FilterBar>
          <PillTabs
            value={filter}
            options={[
              { value: "active", label: "待处理" },
              { value: "all", label: "全部" },
            ]}
            onChange={setFilter}
            label="请求筛选"
          />
        </FilterBar>

        {loading && !data ? (
          <div className="flex justify-center px-5 py-10">
            <LoadingHint loading />
          </div>
        ) : rows.length ? (
          <Table className="min-w-[960px]">
            <TableHeader>
              <TableRow>
                <TableHead>类型</TableHead>
                <TableHead>申请人</TableHead>
                <TableHead>群</TableHead>
                <TableHead>验证消息</TableHead>
                <TableHead>时间</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const Icon = KIND_ICON[row.kind];
                const actionable =
                  data?.enabled !== false &&
                  (row.status === "pending" || row.status === "failed");
                return (
                  <TableRow key={row.id}>
                    <TableCell className="whitespace-nowrap">
                      <Badge variant="outline">
                        <Icon className="mr-1 h-3 w-3" />
                        {KIND_LABEL[row.kind]}
                      </Badge>
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-sm text-slate-800">
                      {requesterLabel(row)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-slate-500">
                      {groupLabel(row)}
                    </TableCell>
                    <TableCell className="max-w-[22rem]">
                      <div className="whitespace-pre-wrap break-words text-sm text-slate-700">
                        {row.comment || <span className="text-slate-400">（无）</span>}
                      </div>
                      {row.error ? (
                        <div className="mt-1 text-[11px] text-red-600">{row.error}</div>
                      ) : null}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-slate-500">
                      {formatTs(row.created_at)}
                      {row.handled_at ? (
                        <div className="mt-0.5 text-[11px] text-slate-400">
                          处理于 {formatTs(row.handled_at)}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      <Badge variant={statusVariant(row.status)}>
                        {STATUS_LABEL[row.status]}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {actionable ? (
                        <div className="flex justify-end gap-2">
                          <Button
                            size="sm"
                            disabled={busyId === row.id}
                            onClick={() => void decide(row, "approve")}
                          >
                            <Check className="h-3.5 w-3.5" />
                            {row.status === "failed" ? "重试同意" : "同意"}
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busyId === row.id}
                            onClick={() => void decide(row, "reject")}
                          >
                            <X className="h-3.5 w-3.5" />
                            拒绝
                          </Button>
                        </div>
                      ) : (
                        <span className="text-xs text-slate-400">
                          {row.status === "queued" || row.status === "executing"
                            ? "等待 bot 执行…"
                            : "-"}
                        </span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          <EmptyState
            icon={Inbox}
            text={filter === "active" ? "暂无待审批的请求。" : "还没有收到任何请求。"}
            hint="好友请求、群邀请和入群申请会自动出现在这里。"
          />
        )}
      </SectionCard>
    </PageStack>
  );
}

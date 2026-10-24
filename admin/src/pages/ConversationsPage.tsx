import { useMemo, useState } from "react";
import { Hash, MessagesSquare, UserRound, Zap } from "lucide-react";

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
import type { ConversationsData } from "@/lib/analytics";
import { formatCompact, formatNumber, formatTs, scopeLabel } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type ScopeFilter = "all" | "group" | "private" | "task";

export default function ConversationsPage({ refreshKey }: { refreshKey: number }) {
  const [scope, setScope] = useState<ScopeFilter>("all");
  const [search, setSearch] = useState("");
  const { data, loading, error } = useApi<ConversationsData>(
    "/api/conversations",
    refreshKey
  );

  const rows = useMemo(() => {
    const all = data?.conversations ?? [];
    const query = search.trim().toLowerCase();
    return all.filter((row) => {
      if (scope !== "all" && row.scope !== scope) return false;
      if (!query) return true;
      return [row.id, row.label, row.sender_name, row.session_id ?? ""].some((value) =>
        value.toLowerCase().includes(query)
      );
    });
  }, [data, scope, search]);

  const all = data?.conversations ?? [];
  const groupCount = all.filter((row) => row.scope === "group").length;
  const privateCount = all.filter((row) => row.scope === "private").length;
  const tokens = rows.reduce((sum, row) => sum + row.tokens, 0);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={MessagesSquare} label="会话总数" value={formatNumber(all.length)} hint="已建立 session 或产生过消息" />
        <StatCard icon={Hash} label="群聊" value={formatNumber(groupCount)} hint="group:*" />
        <StatCard icon={UserRound} label="私聊" value={formatNumber(privateCount)} hint="private:*" />
        <StatCard icon={Zap} label="当前筛选 Token" value={formatCompact(tokens)} hint="累计 token 用量" />
      </div>

      <SectionCard
        icon={MessagesSquare}
        title="会话列表"
        description="合并 chat_events、session_state 与 usage_events"
        badge={rows.length}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-blue-50 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <PillTabs
              value={scope}
              options={[
                { value: "all", label: "全部" },
                { value: "group", label: "群聊" },
                { value: "private", label: "私聊" },
                { value: "task", label: "任务" },
              ]}
              onChange={setScope}
            />
            <SearchField
              value={search}
              onChange={setSearch}
              placeholder="搜索会话 ID、发送者或 session…"
              className="w-64"
            />
          </div>
          {loading ? <span className="text-xs text-slate-500">加载中…</span> : null}
        </div>

        {rows.length ? (
          <Table className="min-w-[860px]">
            <TableHeader>
              <TableRow>
                <TableHead>会话</TableHead>
                <TableHead>类型</TableHead>
                <TableHead className="text-right">入站消息</TableHead>
                <TableHead className="text-right">Token</TableHead>
                <TableHead>当前 Session</TableHead>
                <TableHead className="text-right">会话消息</TableHead>
                <TableHead>最后活跃</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.id}>
                  <TableCell className="max-w-[18rem]">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-slate-800">{row.label}</span>
                      {row.sender_name ? (
                        <span className="truncate text-xs text-muted-foreground">
                          {row.sender_name}
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-0.5 font-mono text-[11px] text-slate-400">{row.id}</div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={row.scope === "group" ? "secondary" : "outline"}>
                      {scopeLabel(row.scope)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {formatNumber(row.messages)}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {formatCompact(row.tokens)}
                  </TableCell>
                  <TableCell className="max-w-[14rem]">
                    {row.session_id ? (
                      <div className="flex items-center gap-1.5">
                        <span className="truncate font-mono text-[11px] text-slate-600" title={row.session_id}>
                          {row.session_id}
                        </span>
                        {row.generation ? (
                          <Badge variant="secondary">g{row.generation}</Badge>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">未建立</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {row.session_id ? formatNumber(row.session_messages) : "-"}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {formatTs(row.last_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <EmptyState
            icon={MessagesSquare}
            text="没有匹配的会话。"
            hint="收到第一条消息、建立 DSH session 或产生模型调用后，这里会出现记录。"
          />
        )}
      </SectionCard>
    </div>
  );
}

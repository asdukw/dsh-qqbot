import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  PauseCircle,
  Radio,
  Rss,
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
import type { FeedsData } from "@/lib/analytics";
import { formatNumber, formatTs, targetLabel } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type FeedFilter = "all" | "errors" | "disabled";

export default function FeedsPage({ refreshKey }: { refreshKey: number }) {
  const [filter, setFilter] = useState<FeedFilter>("all");
  const [search, setSearch] = useState("");
  const { data, loading, error } = useApi<FeedsData>("/api/feeds", refreshKey);

  const rows = useMemo(() => {
    const all = data?.feeds ?? [];
    const query = search.trim().toLowerCase();
    return all.filter((feed) => {
      if (filter === "errors" && !feed.last_error) return false;
      if (filter === "disabled" && feed.enabled) return false;
      if (!query) return true;
      return [feed.title, feed.feed_url, feed.source, String(feed.chat_id)].some((value) =>
        value.toLowerCase().includes(query)
      );
    });
  }, [data, filter, search]);

  const all = data?.feeds ?? [];
  const enabled = all.filter((feed) => feed.enabled).length;
  const seen = all.reduce((sum, feed) => sum + feed.seen_count, 0);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Rss} label="订阅总数" value={formatNumber(all.length)} hint="B 站动态与 RSS/Atom" />
        <StatCard icon={CheckCircle2} label="已启用" value={formatNumber(enabled)} hint="调度器会轮询" tone="emerald" />
        <StatCard
          icon={AlertTriangle}
          label="抓取异常"
          value={formatNumber(data?.errors)}
          hint="last_error 非空"
          tone={data?.errors ? "red" : "slate"}
        />
        <StatCard icon={Radio} label="已见条目" value={formatNumber(seen)} hint="所有订阅的 seen 去重集合" />
      </div>

      <SectionCard
        icon={Rss}
        title="订阅列表"
        description="每个订阅绑定创建时的群或私聊"
        badge={rows.length}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-blue-50 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <PillTabs
              value={filter}
              options={[
                { value: "all", label: "全部" },
                { value: "errors", label: "异常" },
                { value: "disabled", label: "已停用" },
              ]}
              onChange={setFilter}
            />
            <SearchField
              value={search}
              onChange={setSearch}
              placeholder="搜索标题、URL 或目标…"
              className="w-64"
            />
          </div>
          {loading ? <span className="text-xs text-slate-500">加载中…</span> : null}
        </div>

        {rows.length ? (
          <Table className="min-w-[900px]">
            <TableHeader>
              <TableRow>
                <TableHead>状态</TableHead>
                <TableHead>标题 / URL</TableHead>
                <TableHead>来源</TableHead>
                <TableHead>目标</TableHead>
                <TableHead className="text-right">已见</TableHead>
                <TableHead>上次检查</TableHead>
                <TableHead>错误</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((feed) => (
                <TableRow key={feed.id}>
                  <TableCell>
                    {feed.last_error ? (
                      <Badge variant="destructive">
                        <AlertTriangle className="mr-1 h-3 w-3" /> 异常
                      </Badge>
                    ) : !feed.enabled ? (
                      <Badge variant="muted">
                        <PauseCircle className="mr-1 h-3 w-3" /> 停用
                      </Badge>
                    ) : (
                      <Badge variant="success">
                        <CheckCircle2 className="mr-1 h-3 w-3" /> 正常
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="max-w-[22rem]">
                    <div className="truncate font-medium text-slate-800" title={feed.title || feed.feed_url}>
                      {feed.title || "（无标题）"}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[11px] text-slate-400" title={feed.feed_url}>
                      {feed.feed_url}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{feed.source || "rss"}</Badge>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {targetLabel(feed.chat_type, feed.chat_id)}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {formatNumber(feed.seen_count)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {formatTs(feed.last_check)}
                  </TableCell>
                  <TableCell className="max-w-[16rem]">
                    {feed.last_error ? (
                      <span className="line-clamp-2 text-xs text-red-600" title={feed.last_error}>
                        {feed.last_error}
                      </span>
                    ) : (
                      <span className="text-xs text-slate-400">-</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <EmptyState
            icon={Rss}
            text="没有匹配的订阅。"
            hint="在群里或私聊发送 /sub add <B站UID|空间链接|RSS> 即可添加。"
          />
        )}
      </SectionCard>
    </div>
  );
}

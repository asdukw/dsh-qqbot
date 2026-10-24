import { useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Smile, Sticker, Zap } from "lucide-react";

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
import type { StickersData } from "@/lib/analytics";
import { formatBytes, formatNumber, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type StickerFilter = "all" | "ok" | "banned";

export default function StickersPage({ refreshKey }: { refreshKey: number }) {
  const [filter, setFilter] = useState<StickerFilter>("all");
  const [search, setSearch] = useState("");
  const { data, loading, error } = useApi<StickersData>("/api/stickers", refreshKey);

  const rows = useMemo(() => {
    const all = data?.stickers ?? [];
    const query = search.trim().toLowerCase();
    return all.filter((sticker) => {
      if (filter === "ok" && sticker.status !== "ok") return false;
      if (filter === "banned" && sticker.status !== "banned") return false;
      if (!query) return true;
      return [sticker.description, sticker.source, ...sticker.tags].some((value) =>
        value.toLowerCase().includes(query)
      );
    });
  }, [data, filter, search]);

  const all = data?.stickers ?? [];
  const available = all.filter((sticker) => sticker.status === "ok").length;
  const uses = all.reduce((sum, sticker) => sum + sticker.uses, 0);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Sticker} label="表情总数" value={formatNumber(data?.totals.total)} hint="按 sha1 去重" />
        <StatCard icon={CheckCircle2} label="可用" value={formatNumber(available)} hint="status = ok" tone="emerald" />
        <StatCard
          icon={AlertTriangle}
          label="已禁用"
          value={formatNumber(data?.totals.banned)}
          hint="不会被挑中发送"
          tone={data?.totals.banned ? "red" : "slate"}
        />
        <StatCard icon={Zap} label="累计使用" value={formatNumber(uses)} hint={`${formatNumber(data?.totals.used)} 张被用过`} />
      </div>

      <SectionCard
        icon={Sticker}
        title="表情库"
        description="群聊里收集的表情包与模型打标结果"
        badge={rows.length}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-blue-50 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <PillTabs
              value={filter}
              options={[
                { value: "all", label: "全部" },
                { value: "ok", label: "可用" },
                { value: "banned", label: "已禁用" },
              ]}
              onChange={setFilter}
            />
            <SearchField
              value={search}
              onChange={setSearch}
              placeholder="搜索描述、标签或来源…"
              className="w-64"
            />
          </div>
          {loading ? <span className="text-xs text-slate-500">加载中…</span> : null}
        </div>

        {rows.length ? (
          <Table className="min-w-[920px]">
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>描述</TableHead>
                <TableHead>标签</TableHead>
                <TableHead className="text-right">使用</TableHead>
                <TableHead className="text-right">大小</TableHead>
                <TableHead>来源</TableHead>
                <TableHead>加入</TableHead>
                <TableHead>最后使用</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((sticker) => (
                <TableRow key={sticker.id}>
                  <TableCell className="font-mono text-xs text-slate-500">#{sticker.id}</TableCell>
                  <TableCell>
                    {sticker.status === "ok" ? (
                      <Badge variant="success">
                        <CheckCircle2 className="mr-1 h-3 w-3" /> 可用
                      </Badge>
                    ) : (
                      <Badge variant="destructive">
                        <AlertTriangle className="mr-1 h-3 w-3" /> {sticker.status || "异常"}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="max-w-[18rem]">
                    <span className="line-clamp-2 text-sm text-slate-800">
                      {sticker.description || "（未打标）"}
                    </span>
                  </TableCell>
                  <TableCell className="max-w-[14rem]">
                    <div className="flex flex-wrap gap-1">
                      {sticker.tags.length ? (
                        sticker.tags.map((tag) => (
                          <Badge key={tag} variant="secondary" className="text-[10px]">
                            {tag}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-slate-400">-</span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {formatNumber(sticker.uses)}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums text-slate-500">
                    {formatBytes(sticker.size)}
                  </TableCell>
                  <TableCell className="max-w-[12rem]">
                    <div className="truncate text-xs text-slate-600" title={sticker.source}>
                      {sticker.source || "-"}
                    </div>
                    <div className="text-[11px] text-slate-400">{sticker.source_kind || "-"}</div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {formatTs(sticker.created_at)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-slate-500">
                    {sticker.last_used ? formatTs(sticker.last_used) : "-"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <EmptyState
            icon={Smile}
            text="没有匹配的表情。"
            hint="群聊里收到的表情包会自动入库，并由模型打标。"
          />
        )}
      </SectionCard>
    </div>
  );
}

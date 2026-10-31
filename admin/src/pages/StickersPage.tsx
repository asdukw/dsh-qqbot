import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  LayoutGrid,
  List,
  Smile,
  Sticker,
  Zap,
} from "lucide-react";

import {
  EmptyState,
  FilterBar,
  LoadingHint,
  Notice,
  PageStack,
  PillTabs,
  SearchField,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { StickerDetailModal } from "@/components/stickers/StickerDetailModal";
import { StickerThumb } from "@/components/stickers/StickerThumb";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { StickerRow, StickersData } from "@/lib/analytics";
import { requestJson } from "@/lib/api";
import { formatBytes, formatNumber, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type StickerFilter = "all" | "ok" | "banned";
type StickerView = "grid" | "table";
type StickerSort = "default" | "uses-desc" | "uses-asc";

const STATUS_LABELS: Record<string, string> = {
  ok: "可用",
  banned: "已禁用",
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] || "异常";
}

// Icons are looked up in the view toggle so the label stays a plain string.
const VIEW_ICONS: Record<StickerView, typeof LayoutGrid> = {
  grid: LayoutGrid,
  table: List,
};

export default function StickersPage({ refreshKey }: { refreshKey: number }) {
  const [filter, setFilter] = useState<StickerFilter>("all");
  const [view, setView] = useState<StickerView>("grid");
  const [sort, setSort] = useState<StickerSort>("default");
  const [search, setSearch] = useState("");
  const [detailId, setDetailId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(
    null
  );
  const { data, loading, error, reload } = useApi<StickersData>(
    "/api/stickers",
    refreshKey
  );

  const rows = useMemo(() => {
    const all = data?.stickers ?? [];
    const query = search.trim().toLowerCase();
    const filtered = all.filter((sticker) => {
      if (filter === "ok" && sticker.status !== "ok") return false;
      if (filter === "banned" && sticker.status !== "banned") return false;
      if (!query) return true;
      return [sticker.description, sticker.source, ...sticker.tags].some((value) =>
        value.toLowerCase().includes(query)
      );
    });
    if (sort === "uses-desc") {
      return [...filtered].sort((a, b) => b.uses - a.uses || b.id - a.id);
    }
    if (sort === "uses-asc") {
      return [...filtered].sort((a, b) => a.uses - b.uses || b.id - a.id);
    }
    return filtered;
  }, [data, filter, search, sort]);

  const all = data?.stickers ?? [];
  // Derive the open row from the latest payload so a refresh updates the dialog.
  const detail = detailId === null ? null : all.find((sticker) => sticker.id === detailId) ?? null;
  const available = all.filter((sticker) => sticker.status === "ok").length;
  const uses = all.reduce((sum, sticker) => sum + sticker.uses, 0);

  async function deleteSticker(sticker: StickerRow, blockSimilar: boolean) {
    const suffix = blockSimilar ? "，并删除/屏蔽所有相似图片" : "";
    if (!window.confirm(`删除表情 #${sticker.id}${suffix}？此操作不可恢复。`)) {
      return;
    }
    setDeleting(true);
    setNotice(null);
    try {
      const result = await requestJson<{ deleted: number; blocked: boolean }>(
        "/api/stickers/delete",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: sticker.id, block_similar: blockSimilar }),
        }
      );
      setDetailId(null);
      setNotice({
        kind: "ok",
        text: `已删除 ${result.deleted} 张${result.blocked ? "，并已屏蔽相似图片" : ""}。`,
      });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: err instanceof Error ? err.message : "删除失败",
      });
    } finally {
      setDeleting(false);
    }
  }

  return (
    <>
      <PageStack error={error} loading={loading} hasData={!!data}>
        {notice ? (
          <Notice tone={notice.kind === "ok" ? "success" : "danger"}>{notice.text}</Notice>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            icon={Sticker}
            label="表情总数"
            value={formatNumber(data?.totals.total)}
            hint="含已禁用"
          />
          <StatCard
            icon={CheckCircle2}
            label="可用"
            value={formatNumber(available)}
            hint="可正常发送"
            tone="emerald"
          />
          <StatCard
            icon={AlertTriangle}
            label="已禁用"
            value={formatNumber(data?.totals.banned)}
            hint="不会被挑中发送"
            tone={data?.totals.banned ? "red" : "slate"}
          />
          <StatCard
            icon={Zap}
            label="累计使用"
            value={formatNumber(uses)}
            hint={`${formatNumber(data?.totals.used)} 张被用过`}
          />
        </div>

        <SectionCard icon={Sticker} title="全部表情" badge={rows.length}>
          <FilterBar
            actions={
              <div className="flex items-center gap-2">
                <LoadingHint loading={loading} />
                <Select
                  value={sort}
                  onChange={(event) => setSort(event.target.value as StickerSort)}
                  aria-label="排序方式"
                  className="w-32"
                >
                  <option value="default">默认排序</option>
                  <option value="uses-desc">使用最多</option>
                  <option value="uses-asc">使用最少</option>
                </Select>
                <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5">
                  {(Object.keys(VIEW_ICONS) as StickerView[]).map((option) => {
                    const Icon = VIEW_ICONS[option];
                    const active = view === option;
                    const label = option === "grid" ? "网格视图" : "列表视图";
                    return (
                      <button
                        key={option}
                        type="button"
                        onClick={() => setView(option)}
                        aria-pressed={active}
                        aria-label={label}
                        title={label}
                        className={cn(
                          "inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                          active
                            ? "bg-primary text-white shadow-sm"
                            : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
                        )}
                      >
                        <Icon className="h-3.5 w-3.5" />
                        {option === "grid" ? "网格" : "列表"}
                      </button>
                    );
                  })}
                </div>
              </div>
            }
          >
            <PillTabs
              value={filter}
              options={[
                { value: "all", label: "全部" },
                { value: "ok", label: "可用" },
                { value: "banned", label: "已禁用" },
              ]}
              onChange={setFilter}
              label="状态筛选"
            />
            <SearchField
              value={search}
              onChange={setSearch}
              placeholder="搜索描述、标签或来源…"
              className="w-64"
            />
          </FilterBar>

          {rows.length ? (
            view === "grid" ? (
              <div className="grid grid-cols-2 gap-3 p-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 2xl:grid-cols-6">
                {rows.map((sticker) => (
                  <button
                    key={sticker.id}
                    type="button"
                    onClick={() => setDetailId(sticker.id)}
                    className={cn(
                      "group flex flex-col overflow-hidden rounded-xl border bg-white text-left transition",
                      "hover:shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                      sticker.status === "banned"
                        ? "border-red-200 hover:border-red-300"
                        : "border-slate-200 hover:border-slate-300"
                    )}
                  >
                    <div className="relative aspect-square w-full overflow-hidden bg-slate-50">
                      <StickerThumb
                        id={sticker.id}
                        hasImage={sticker.has_image}
                        alt={sticker.description || `表情 #${sticker.id}`}
                        className="h-full w-full object-contain p-2 transition-transform duration-200 group-hover:scale-[1.04]"
                        iconClassName="h-8 w-8"
                        refreshKey={refreshKey}
                      />
                      <div className="absolute inset-x-1.5 top-1.5 flex items-start gap-1">
                        {sticker.status === "banned" ? (
                          <Badge
                            variant="destructive"
                            className="bg-white/90 text-[10px] shadow-sm backdrop-blur"
                          >
                            <AlertTriangle className="mr-1 h-3 w-3" /> 已禁用
                          </Badge>
                        ) : null}
                        <span className="ml-auto rounded-full bg-white/90 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 shadow-sm backdrop-blur">
                          #{sticker.id}
                        </span>
                      </div>
                    </div>
                    <div className="w-full space-y-1 px-2.5 py-2">
                      <p
                        className="line-clamp-1 text-xs font-medium text-slate-800"
                        title={sticker.description || undefined}
                      >
                        {sticker.description || "未打标"}
                      </p>
                      {sticker.tags.length ? (
                        <div className="flex items-center gap-1 overflow-hidden">
                          {sticker.tags.slice(0, 2).map((tag) => (
                            <span
                              key={tag}
                              className="truncate rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600"
                            >
                              {tag}
                            </span>
                          ))}
                          {sticker.tags.length > 2 ? (
                            <span className="shrink-0 text-[10px] text-slate-400">
                              +{sticker.tags.length - 2}
                            </span>
                          ) : null}
                        </div>
                      ) : null}
                      <div className="flex items-center justify-between gap-2 text-[10px] tabular-nums text-slate-500">
                        <span>用 {formatNumber(sticker.uses)} 次</span>
                        <span>{formatBytes(sticker.size)}</span>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <Table className="min-w-[980px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>预览</TableHead>
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
                      <TableCell>
                        <button
                          type="button"
                          onClick={() => setDetailId(sticker.id)}
                          title="查看大图"
                          aria-label={`查看表情 #${sticker.id}`}
                          className="block h-11 w-11 overflow-hidden rounded-lg border border-slate-200 bg-slate-50 transition hover:border-slate-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                        >
                          <StickerThumb
                            id={sticker.id}
                            hasImage={sticker.has_image}
                            alt={sticker.description || `表情 #${sticker.id}`}
                            className="h-full w-full object-contain"
                            iconClassName="h-4 w-4"
                            refreshKey={refreshKey}
                          />
                        </button>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-slate-500">#{sticker.id}</TableCell>
                      <TableCell>
                        {sticker.status === "ok" ? (
                          <Badge variant="success">
                            <CheckCircle2 className="mr-1 h-3 w-3" /> 可用
                          </Badge>
                        ) : (
                          <Badge variant="destructive">
                            <AlertTriangle className="mr-1 h-3 w-3" /> {statusLabel(sticker.status)}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="max-w-[18rem]">
                        <span className="line-clamp-2 text-sm text-slate-700">
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
            )
          ) : (
            <EmptyState
              icon={Smile}
              text="没有匹配的表情。"
              hint="收到的表情会自动入库。"
            />
          )}
        </SectionCard>
      </PageStack>

      <StickerDetailModal
        key={detail?.id ?? "none"}
        sticker={detail}
        refreshKey={refreshKey}
        deleting={deleting}
        onDelete={(sticker, blockSimilar) => void deleteSticker(sticker, blockSimilar)}
        onClose={() => setDetailId(null)}
      />
    </>
  );
}

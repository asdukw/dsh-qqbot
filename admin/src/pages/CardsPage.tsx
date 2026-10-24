import { useMemo, useState } from "react";
import { CalendarClock, CheckCircle2, CreditCard, Eye, Hash } from "lucide-react";

import {
  EmptyState,
  ErrorBanner,
  SearchField,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { CardDetailModal } from "@/components/cards/CardDetailModal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { CardsData } from "@/lib/analytics";
import { formatDate, formatNumber } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

export default function CardsPage({ refreshKey }: { refreshKey: number }) {
  const [search, setSearch] = useState("");
  const [detailId, setDetailId] = useState<number | null>(null);
  const { data, loading, error } = useApi<CardsData>("/api/cards", refreshKey);

  const rows = useMemo(() => {
    const all = data?.cards ?? [];
    const query = search.trim().toLowerCase();
    if (!query) return all;
    return all.filter((card) =>
      [
        card.name,
        card.nickname,
        card.description,
        card.creator,
        ...card.aliases,
        ...card.tags,
      ].some((value) => value.toLowerCase().includes(query))
    );
  }, [data, search]);

  const all = data?.cards ?? [];
  const aliases = all.reduce((sum, card) => sum + card.aliases.length, 0);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={CreditCard} label="角色卡总数" value={formatNumber(all.length)} hint="SillyTavern V1 / V2 / V3" />
        <StatCard icon={CheckCircle2} label="当前角色卡" value={data?.active_card ?? "-"} hint="active card" tone="emerald" />
        <StatCard icon={Hash} label="别名总数" value={formatNumber(aliases)} hint="可用于点名与主动接话" />
        <StatCard
          icon={CalendarClock}
          label="最近导入"
          value={all.length ? formatDate(Math.max(...all.map((card) => card.updated_at))) : "-"}
          hint="updated_at"
        />
      </div>

      <SectionCard
        icon={CreditCard}
        title="角色卡库"
        description="当前角色卡会随 /card use 热切换，点击卡片查看 V3 规范详情"
        badge={rows.length}
        actions={
          <SearchField
            value={search}
            onChange={setSearch}
            placeholder="搜索名称、标签、作者或别名…"
            className="w-64"
          />
        }
        contentClassName="p-5"
      >
        {rows.length ? (
          <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
            {rows.map((card) => (
              <Card
                key={card.id}
                className={cn(
                  "flex cursor-pointer flex-col transition-shadow hover:shadow-md focus-within:shadow-md",
                  card.is_active && "border-blue-300 ring-1 ring-blue-200"
                )}
                onClick={() => setDetailId(card.id)}
              >
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <CardTitle
                        className="cursor-pointer truncate hover:text-blue-700"
                        role="button"
                        tabIndex={0}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setDetailId(card.id);
                          }
                        }}
                      >
                        {card.nickname || card.name}
                      </CardTitle>
                      <CardDescription className="mt-1 truncate">
                        #{card.id} · {card.name}
                        {card.character_version ? ` · v${card.character_version}` : ""}
                      </CardDescription>
                    </div>
                    {card.is_active ? <Badge>当前</Badge> : null}
                  </div>
                </CardHeader>
                <CardContent className="flex flex-1 flex-col gap-3 pt-0">
                  <p className="line-clamp-3 min-h-[3.6rem] text-sm text-slate-600">
                    {card.description || "没有简介。"}
                  </p>

                  <div className="space-y-1.5">
                    <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">别名</p>
                    <div className="flex flex-wrap gap-1.5">
                      {card.aliases.length ? (
                        card.aliases.map((alias) => (
                          <Badge key={alias} variant="secondary">
                            {alias}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-muted-foreground">无别名</span>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-1.5">
                    {card.tags.map((tag) => (
                      <Badge key={tag} variant="outline" className="text-[10px]">
                        {tag}
                      </Badge>
                    ))}
                  </div>

                  <div className="mt-auto space-y-1 border-t border-blue-50 pt-3 text-[11px] text-slate-400">
                    <div className="flex justify-between gap-2">
                      <span>作者</span>
                      <span className="truncate text-slate-600">{card.creator || "-"}</span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span>来源</span>
                      <span className="truncate font-mono text-slate-500" title={card.source}>
                        {card.source_kind || "file"} · {card.source || "-"}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span>更新时间</span>
                      <span className="text-slate-600">{formatDate(card.updated_at)}</span>
                    </div>
                  </div>

                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full"
                    onClick={(event) => {
                      event.stopPropagation();
                      setDetailId(card.id);
                    }}
                  >
                    <Eye className="h-3.5 w-3.5" />
                    查看详情
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={CreditCard}
            text="没有匹配的角色卡。"
            hint="用 /card install <url|路径> 导入 SillyTavern 角色卡。"
          />
        )}
      </SectionCard>

      <CardDetailModal cardId={detailId} onClose={() => setDetailId(null)} />
    </div>
  );
}

import { useMemo, useState } from "react";
import { CheckCircle2, CreditCard } from "lucide-react";

import {
  EmptyState,
  PageStack,
  SearchField,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { CardDetailModal } from "@/components/cards/CardDetailModal";
import { Badge } from "@/components/ui/badge";
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

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      <div className="grid gap-3 sm:grid-cols-2">
        <StatCard
          icon={CreditCard}
          label="角色卡总数"
          value={formatNumber(all.length)}
          hint="SillyTavern V1 / V2 / V3"
        />
        <StatCard
          icon={CheckCircle2}
          label="当前角色卡"
          value={data?.active_card || "-"}
          tone="emerald"
        />
      </div>

      <SectionCard
        icon={CreditCard}
        title="角色卡库"
        badge={rows.length}
        actions={
          <SearchField
            value={search}
            onChange={setSearch}
            placeholder="搜索名称、标签、作者或别名…"
            className="w-full sm:w-64"
          />
        }
        contentClassName="p-4"
      >
        {rows.length ? (
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
            {rows.map((card) => (
              <div
                key={card.id}
                className={cn(
                  "relative flex flex-col rounded-xl border bg-white transition-colors",
                  card.is_active
                    ? "border-blue-300 ring-1 ring-blue-200"
                    : "border-slate-200 hover:border-slate-300"
                )}
              >
                <button
                  type="button"
                  onClick={() => setDetailId(card.id)}
                  aria-label={`查看角色卡 ${card.nickname || card.name}`}
                  className="absolute inset-0 z-10 rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                />
                <div className="pointer-events-none flex flex-1 flex-col gap-3 p-4">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-slate-900">
                        {card.nickname || card.name}
                      </h3>
                      <p className="mt-0.5 truncate text-xs tabular-nums text-slate-500">
                        #{card.id} · {card.name}
                        {card.character_version ? ` · v${card.character_version}` : ""}
                      </p>
                    </div>
                    {card.is_active ? <Badge>当前</Badge> : null}
                  </div>

                  <p className="line-clamp-3 min-h-[3.6rem] text-sm text-slate-700">
                    {card.description || "没有简介。"}
                  </p>

                  <div className="space-y-1.5">
                    <p className="text-xs text-slate-500">别名</p>
                    <div className="flex flex-wrap gap-1.5">
                      {card.aliases.length ? (
                        card.aliases.map((alias) => (
                          <Badge key={alias} variant="secondary">
                            {alias}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-slate-500">无别名</span>
                      )}
                    </div>
                  </div>

                  {card.tags.length ? (
                    <div className="flex flex-wrap gap-1.5">
                      {card.tags.map((tag) => (
                        <Badge key={tag} variant="outline">
                          {tag}
                        </Badge>
                      ))}
                    </div>
                  ) : null}

                  <dl className="mt-auto space-y-1 border-t border-slate-100 pt-3 text-xs">
                    <div className="flex justify-between gap-2">
                      <dt className="text-slate-500">作者</dt>
                      <dd className="truncate text-slate-700">{card.creator || "-"}</dd>
                    </div>
                    <div className="flex justify-between gap-2">
                      <dt className="text-slate-500">更新时间</dt>
                      <dd className="tabular-nums text-slate-700">
                        {formatDate(card.updated_at)}
                      </dd>
                    </div>
                  </dl>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={CreditCard}
            text="没有匹配的角色卡。"
            hint={"用 /card install <url|路径> 导入 SillyTavern 角色卡。"}
          />
        )}
      </SectionCard>

      <CardDetailModal cardId={detailId} onClose={() => setDetailId(null)} />
    </PageStack>
  );
}

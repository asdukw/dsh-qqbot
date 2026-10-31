import { MessageSquareText, Sparkles, Trophy } from "lucide-react";

import WordCloud from "@/components/WordCloud";
import { EmptyState, LoadingHint, SectionCard } from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import { formatNumber } from "@/lib/format";
import type { FunData } from "@/lib/analytics";

export default function FunPanel({
  data,
  loading,
}: {
  data: FunData | null;
  loading: boolean;
}) {
  const maxRanking = Math.max(1, ...(data?.ranking.map((row) => row.messages) ?? [1]));
  const cloudWords = (data?.words ?? []).slice(0, 40);
  const cloudSeed = data
    ? `${data.start}|${data.end}|${data.conversation_id}`
    : "";
  const initialLoading = loading && !data;

  return (
    <div className="grid gap-5 xl:grid-cols-[1fr_1.35fr]">
      <SectionCard
        icon={Trophy}
        title="发言排行"
        description="按消息数排序"
        actions={<LoadingHint loading={loading} />}
        contentClassName="space-y-3 p-5"
      >
        {initialLoading ? (
          <div className="flex min-h-[200px] items-center justify-center">
            <LoadingHint loading />
          </div>
        ) : data?.ranking.length ? (
          data.ranking.map((row, index) => (
            <div key={row.user_id} className="space-y-1.5">
              <div className="flex items-center justify-between gap-3 text-sm">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="w-5 shrink-0 text-xs font-semibold tabular-nums text-slate-400">
                    {index + 1}
                  </span>
                  <span className="truncate font-medium text-slate-700">{row.name}</span>
                  {index === 0 ? <Badge variant="secondary">话痨</Badge> : null}
                </div>
                <span className="shrink-0 text-xs tabular-nums text-slate-500">
                  {formatNumber(row.messages)} 条
                </span>
              </div>
              <div className="ml-7 h-1.5 overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(3, (row.messages / maxRanking) * 100)}%`,
                    backgroundColor: "var(--chart-1)",
                  }}
                />
              </div>
            </div>
          ))
        ) : (
          <EmptyState icon={MessageSquareText} text="这个范围内还没有消息。" />
        )}
      </SectionCard>

      <SectionCard
        icon={Sparkles}
        title="词云"
        description="字号越大，提到该词的消息越多"
        badge={cloudWords.length}
        contentClassName="p-5"
      >
        {initialLoading ? (
          <div className="flex min-h-[260px] items-center justify-center">
            <LoadingHint loading />
          </div>
        ) : data?.words.length ? (
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-2 py-4 sm:px-4">
            <div className="sm:hidden">
              <WordCloud words={cloudWords} seed={cloudSeed} compact />
            </div>
            <div className="hidden sm:block">
              <WordCloud words={cloudWords} seed={cloudSeed} />
            </div>
          </div>
        ) : (
          <EmptyState
            icon={Sparkles}
            text="这个范围内还没有足够的聊天文本。"
          />
        )}
      </SectionCard>
    </div>
  );
}

import { Loader2, MessageSquareText, Sparkles, Trophy } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
  const maxWord = Math.max(1, ...(data?.words.map((word) => word.count) ?? [1]));

  return (
    <div className="grid gap-6 xl:grid-cols-[1fr_1.35fr]">
      <Card className="overflow-hidden">
        <CardHeader className="flex-row items-start justify-between space-y-0 border-b border-blue-50 pb-4">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
              <Trophy className="h-5 w-5" />
            </div>
            <div>
              <CardTitle>今日发言排行</CardTitle>
              <CardDescription className="mt-1">
                {data?.date ?? "今天"} · {formatNumber(data?.messages)} 条消息
              </CardDescription>
            </div>
          </div>
          {loading ? <Loader2 className="h-4 w-4 animate-spin text-blue-600" /> : null}
        </CardHeader>
        <CardContent className="space-y-3 p-5">
          {data?.ranking.length ? (
            data.ranking.map((row, index) => (
              <div key={row.user_id} className="space-y-1.5">
                <div className="flex items-center justify-between gap-3 text-sm">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="w-5 shrink-0 text-xs font-semibold text-slate-400">
                      {index + 1}
                    </span>
                    <span className="truncate font-medium text-slate-700">{row.name}</span>
                    {index === 0 ? <Badge variant="secondary">今日话痨</Badge> : null}
                  </div>
                  <span className="shrink-0 text-xs text-slate-500">
                    {formatNumber(row.messages)} 条
                  </span>
                </div>
                <div className="ml-7 h-1.5 overflow-hidden rounded-full bg-blue-50">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-blue-500 to-sky-400"
                    style={{ width: `${Math.max(3, (row.messages / maxRanking) * 100)}%` }}
                  />
                </div>
              </div>
            ))
          ) : (
            <div className="flex flex-col items-center justify-center gap-2 py-10 text-center text-sm text-muted-foreground">
              <MessageSquareText className="h-6 w-6 text-blue-200" />
              <p>今天还没有新的群聊记录。</p>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader className="flex-row items-start justify-between space-y-0 border-b border-blue-50 pb-4">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <CardTitle>今日词云</CardTitle>
              <CardDescription className="mt-1">
                取今天最近 2000 条消息，按词频放大
              </CardDescription>
            </div>
          </div>
          <Badge variant="outline">{data?.words.length ?? 0} 个词</Badge>
        </CardHeader>
        <CardContent className="p-5">
          {data?.words.length ? (
            <div className="flex min-h-[260px] flex-wrap content-center items-baseline justify-center gap-x-4 gap-y-2 rounded-xl bg-gradient-to-br from-blue-50/80 via-white to-sky-50 px-4 py-6">
              {data.words.map((word, index) => {
                const scale = word.count / maxWord;
                return (
                  <span
                    key={word.text}
                    title={`${word.text} · ${word.count} 次`}
                    className={
                      index % 4 === 0
                        ? "font-semibold text-blue-700"
                        : index % 4 === 1
                          ? "font-medium text-blue-600"
                          : index % 4 === 2
                            ? "font-medium text-sky-600"
                            : "font-medium text-indigo-500"
                    }
                    style={{
                      fontSize: `${12 + Math.round(scale * 30)}px`,
                      opacity: 0.62 + scale * 0.38,
                      lineHeight: 1.1,
                    }}
                  >
                    {word.text}
                  </span>
                );
              })}
            </div>
          ) : (
            <div className="flex min-h-[260px] flex-col items-center justify-center gap-2 text-center text-sm text-muted-foreground">
              <Sparkles className="h-6 w-6 text-blue-200" />
              <p>今天还没有足够的聊天文本。</p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

import { useMemo, useState } from "react";
import { CheckCircle2, Hash, PauseCircle, Server, Zap } from "lucide-react";

import {
  EmptyState,
  ErrorBanner,
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
import type { ModelsData } from "@/lib/analytics";
import { formatNumber, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

export default function ModelsPage({ refreshKey }: { refreshKey: number }) {
  const [search, setSearch] = useState("");
  const { data, loading, error } = useApi<ModelsData>("/api/models", refreshKey);

  const rows = useMemo(() => {
    const all = data?.models ?? [];
    const query = search.trim().toLowerCase();
    if (!query) return all;
    return all.filter((model) =>
      [model.provider, model.model_id, model.display_name, model.base_url].some((value) =>
        value.toLowerCase().includes(query)
      )
    );
  }, [data, search]);

  const all = data?.models ?? [];
  const enabled = all.filter((model) => model.enabled).length;
  const active = all.find((model) => model.is_active);

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Server} label="模型总数" value={formatNumber(all.length)} hint="SQLite 注册表" />
        <StatCard icon={CheckCircle2} label="已启用" value={formatNumber(enabled)} hint="可用于 /model 切换" tone="emerald" />
        <StatCard
          icon={Zap}
          label="当前模型"
          value={active?.model_id ?? data?.active ?? "-"}
          hint={active ? active.provider : "DSH route"}
        />
        <StatCard
          icon={Hash}
          label="当前上下文"
          value={active ? formatNumber(active.context_window) : "-"}
          hint="tokens"
        />
      </div>

      <SectionCard
        icon={Server}
        title="模型注册表"
        description="模型、端点与推理参数由 SQLite 统一管理"
        badge={rows.length}
        actions={
          <SearchField
            value={search}
            onChange={setSearch}
            placeholder="搜索 provider、model 或端点…"
            className="w-60"
          />
        }
      >
        {rows.length ? (
          <Table className="min-w-[980px]">
            <TableHeader>
              <TableRow>
                <TableHead>模型</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">上下文</TableHead>
                <TableHead className="text-right">最大输出</TableHead>
                <TableHead>Reasoning</TableHead>
                <TableHead>API</TableHead>
                <TableHead>Base URL</TableHead>
                <TableHead>更新</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((model) => {
                const capabilities = Object.entries(model.capabilities).filter(([, value]) => Boolean(value));
                return (
                  <TableRow key={`${model.provider}/${model.model_id}`}>
                    <TableCell className="max-w-[20rem]">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium text-slate-800">{model.display_name}</span>
                        {model.is_active ? <Badge>当前</Badge> : null}
                        {!model.enabled ? <Badge variant="muted">禁用</Badge> : null}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-slate-400">
                        {model.provider}/{model.model_id}
                      </div>
                      {capabilities.length ? (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {capabilities.map(([key]) => (
                            <Badge key={key} variant="outline" className="text-[10px]">
                              {key}
                            </Badge>
                          ))}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      {model.enabled ? (
                        <Badge variant="success">
                          <CheckCircle2 className="mr-1 h-3 w-3" /> 启用
                        </Badge>
                      ) : (
                        <Badge variant="muted">
                          <PauseCircle className="mr-1 h-3 w-3" /> 禁用
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {formatNumber(model.context_window)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {formatNumber(model.max_output_tokens)}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{model.reasoning_effort || "-"}</Badge>
                    </TableCell>
                    <TableCell className="whitespace-nowrap font-mono text-[11px] text-slate-500">
                      {model.api}
                    </TableCell>
                    <TableCell className="max-w-[18rem] truncate font-mono text-[11px] text-slate-500" title={model.base_url}>
                      {model.base_url}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-slate-500">
                      {formatTs(model.updated_at)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          <EmptyState icon={Server} text="没有匹配的模型。" />
        )}
      </SectionCard>
    </div>
  );
}

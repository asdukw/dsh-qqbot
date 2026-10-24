import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowLeft,
  Boxes,
  CheckCircle2,
  CircleSlash2,
  Hash,
  KeyRound,
  Pencil,
  Plus,
  Power,
  Server,
  Trash2,
  Zap,
} from "lucide-react";

import {
  EmptyState,
  ErrorBanner,
  SearchField,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { ModelForm, type ModelFormValues } from "@/components/models/ModelForm";
import { ProviderForm, type ProviderFormValues } from "@/components/models/ProviderForm";
import { ProviderIcon } from "@/components/models/ProviderIcon";
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
import type { ModelRow, ModelsData, ProviderRow } from "@/lib/analytics";
import { requestJson } from "@/lib/api";
import { formatNumber, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type ProviderModalState =
  | { mode: "create" }
  | { mode: "edit"; provider: ProviderRow }
  | null;

type ModelModalState =
  | { mode: "create" }
  | { mode: "edit"; model: ModelRow }
  | null;

type Notice = { kind: "ok" | "error"; text: string } | null;

export default function ModelsPage({ refreshKey }: { refreshKey: number }) {
  const { data, loading, error, reload } = useApi<ModelsData>("/api/models", refreshKey);
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [providerModal, setProviderModal] = useState<ProviderModalState>(null);
  const [modelModal, setModelModal] = useState<ModelModalState>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState<Notice>(null);

  const providers = data?.providers ?? [];
  const models = data?.models ?? [];
  const selectedProvider = providers.find((item) => item.provider === selected) ?? null;
  const selectedModels = useMemo(
    () => models.filter((model) => model.provider === selected),
    [models, selected]
  );

  const visibleProviders = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return providers;
    return providers.filter((provider) =>
      [provider.provider, provider.display_name, provider.base_url].some(
        (value) => value.toLowerCase().includes(query)
      )
    );
  }, [providers, search]);

  const enabledProviders = providers.filter((provider) => provider.enabled).length;
  const enabledModels = models.filter((model) => model.enabled).length;
  const activeModel = models.find((model) => model.is_active);

  async function call(path: string, payload: unknown, inForm = false): Promise<boolean> {
    setBusy(true);
    if (inForm) setFormError("");
    else setNotice(null);
    try {
      await requestJson<ModelsData>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      reload();
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : "操作失败";
      if (inForm) setFormError(message);
      else setNotice({ kind: "error", text: message });
      return false;
    } finally {
      setBusy(false);
    }
  }

  function openCreateProvider() {
    setFormError("");
    setNotice(null);
    setProviderModal({ mode: "create" });
  }

  function openEditProvider(provider: ProviderRow) {
    setFormError("");
    setNotice(null);
    setProviderModal({ mode: "edit", provider });
  }

  function openCreateModel() {
    setFormError("");
    setNotice(null);
    setModelModal({ mode: "create" });
  }

  function openEditModel(model: ModelRow) {
    setFormError("");
    setNotice(null);
    setModelModal({ mode: "edit", model });
  }

  async function submitProvider(values: ProviderFormValues) {
    const editing = providerModal?.mode === "edit";
    const ok = await call("/api/models/providers", values, true);
    if (!ok) return;
    setProviderModal(null);
    setSelected(values.provider);
    setNotice({
      kind: "ok",
      text: editing
        ? `供应商 ${values.provider} 已更新`
        : `供应商 ${values.provider} 已添加，现在可以添加模型`,
    });
  }

  async function submitModel(values: ModelFormValues) {
    if (!selectedProvider) return;
    const editing = modelModal?.mode === "edit";
    const ok = await call("/api/models/items", values, true);
    if (!ok) return;
    setModelModal(null);
    setNotice({
      kind: "ok",
      text: editing
        ? `模型 ${values.provider}/${values.model_id} 已更新`
        : `模型 ${values.provider}/${values.model_id} 已添加`,
    });
  }

  async function activateModel(model: ModelRow) {
    const ok = await call("/api/models/active", {
      provider: model.provider,
      model_id: model.model_id,
    });
    if (ok) {
      setNotice({
        kind: "ok",
        text: `当前模型已切换到 ${model.provider}/${model.model_id}，下一条消息生效`,
      });
    }
  }

  async function changeReasoning(model: ModelRow, value: string) {
    const ok = await call("/api/models/items", {
      provider: model.provider,
      model_id: model.model_id,
      reasoning_effort: value,
    });
    if (ok) {
      setNotice({
        kind: "ok",
        text: `模型 ${model.model_id} 的推理强度已设为 ${value || "模型默认"}`,
      });
    }
  }

  async function toggleModel(model: ModelRow) {
    const ok = await call("/api/models/items", {
      provider: model.provider,
      model_id: model.model_id,
      enabled: !model.enabled,
    });
    if (ok) {
      setNotice({
        kind: "ok",
        text: `模型 ${model.provider}/${model.model_id} 已${model.enabled ? "禁用" : "启用"}`,
      });
    }
  }

  async function deleteModel(model: ModelRow) {
    if (
      !window.confirm(
        `删除模型 ${model.provider}/${model.model_id}？\n\n删除后不可恢复；仍在引用该模型的旧 DSH session 可能无法继续。`
      )
    )
      return;
    const ok = await call("/api/models/items/delete", {
      provider: model.provider,
      model_id: model.model_id,
    });
    if (ok) setNotice({ kind: "ok", text: `模型 ${model.model_id} 已删除` });
  }

  async function toggleProvider(provider: ProviderRow) {
    const ok = await call("/api/models/providers", {
      provider: provider.provider,
      enabled: !provider.enabled,
    });
    if (ok) {
      setNotice({
        kind: "ok",
        text: `供应商 ${provider.provider} 已${provider.enabled ? "禁用" : "启用"}`,
      });
    }
  }

  async function deleteProvider(provider: ProviderRow) {
    if (provider.model_count > 0) return;
    if (!window.confirm(`删除供应商 ${provider.provider}？`)) return;
    const ok = await call("/api/models/providers/delete", {
      provider: provider.provider,
    });
    if (ok) {
      setSelected(null);
      setNotice({ kind: "ok", text: `供应商 ${provider.provider} 已删除` });
    }
  }

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}
      {notice ? (
        <div
          className={cn(
            "flex items-start gap-2 rounded-xl border px-4 py-3 text-sm",
            notice.kind === "ok"
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border-red-200 bg-red-50 text-red-700"
          )}
        >
          {notice.kind === "ok" ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <CircleSlash2 className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <span>{notice.text}</span>
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Boxes}
          label="供应商"
          value={formatNumber(providers.length)}
          hint={`${enabledProviders} 个已启用 · 端点与 key 配置`}
        />
        <StatCard
          icon={Server}
          label="模型总数"
          value={formatNumber(models.length)}
          hint="SQLite 注册表"
        />
        <StatCard
          icon={CheckCircle2}
          label="已启用"
          value={formatNumber(enabledModels)}
          hint="可用于 /model 切换"
          tone="emerald"
        />
        <StatCard
          icon={Zap}
          label="当前模型"
          value={activeModel?.model_id ?? data?.active ?? "-"}
          hint={activeModel ? activeModel.provider_display_name || activeModel.provider : "DSH route"}
        />
      </div>

      {selectedProvider ? (
        <ProviderDetail
          provider={selectedProvider}
          models={selectedModels}
          busy={busy}
          onBack={() => {
            setSelected(null);
            setNotice(null);
          }}
          onEdit={() => openEditProvider(selectedProvider)}
          onToggle={() => void toggleProvider(selectedProvider)}
          onDelete={() => void deleteProvider(selectedProvider)}
          onAddModel={openCreateModel}
          onEditModel={openEditModel}
          onActivateModel={(model) => void activateModel(model)}
          onChangeReasoning={(model, value) => void changeReasoning(model, value)}
          onToggleModel={(model) => void toggleModel(model)}
          onDeleteModel={(model) => void deleteModel(model)}
        />
      ) : (
        <SectionCard
          icon={Boxes}
          title="供应商"
          description="先添加供应商，再进入供应商添加模型"
          badge={visibleProviders.length}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <SearchField
                value={search}
                onChange={setSearch}
                placeholder="搜索供应商或端点…"
                className="w-56"
              />
              <Button size="sm" onClick={openCreateProvider}>
                <Plus className="h-4 w-4" />
                添加供应商
              </Button>
            </div>
          }
          contentClassName="p-5"
        >
          {visibleProviders.length ? (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {visibleProviders.map((provider) => (
                <ProviderCard
                  key={provider.provider}
                  provider={provider}
                  onClick={() => {
                    setNotice(null);
                    setSelected(provider.provider);
                  }}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Boxes}
              text={providers.length ? "没有匹配的供应商。" : "还没有供应商。"}
              hint="添加供应商后，点进供应商卡片即可登记该供应商的模型。"
            />
          )}
        </SectionCard>
      )}

      <ProviderForm
        open={providerModal !== null}
        mode={providerModal?.mode ?? "create"}
        provider={providerModal?.mode === "edit" ? providerModal.provider : undefined}
        presets={data?.presets ?? []}
        existingProviders={providers.map((provider) => provider.provider)}
        busy={busy}
        error={formError}
        onClose={() => setProviderModal(null)}
        onSubmit={(values) => void submitProvider(values)}
      />

      {selectedProvider ? (
        <ModelForm
          open={modelModal !== null}
          mode={modelModal?.mode ?? "create"}
          provider={selectedProvider}
          model={modelModal?.mode === "edit" ? modelModal.model : undefined}
          busy={busy}
          error={formError}
          onClose={() => setModelModal(null)}
          onSubmit={(values) => void submitModel(values)}
        />
      ) : null}
    </div>
  );
}

function ProviderCard({
  provider,
  onClick,
}: {
  provider: ProviderRow;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group flex h-full flex-col rounded-xl border border-blue-100 bg-white p-4 text-left transition-all hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-md"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-3">
          <ProviderIcon provider={provider.provider} className="h-11 w-11" />
          <div className="min-w-0">
            <p className="truncate font-medium text-slate-800">{provider.display_name}</p>
            <p className="mt-0.5 font-mono text-[11px] text-slate-400">{provider.provider}</p>
          </div>
        </div>
        <Badge variant={provider.enabled ? "success" : "muted"}>
          {provider.enabled ? "启用" : "禁用"}
        </Badge>
      </div>

      <p className="mt-3 truncate font-mono text-[11px] text-slate-500" title={provider.base_url}>
        {provider.base_url || "未配置 base_url"}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Badge variant="outline" className="text-[10px]">
          {provider.model_count} 个模型
        </Badge>
        <Badge
          variant={provider.api_key_set ? "success" : "outline"}
          className="text-[10px]"
        >
          <KeyRound className="mr-1 h-3 w-3" />
          {provider.api_key_source === "stored"
            ? "Key 已保存"
            : provider.api_key_source === "env"
              ? "Key 已注入"
              : "Key 未设置"}
        </Badge>
        {provider.active_model_id ? (
          <Badge className="text-[10px]">当前 {provider.active_model_id}</Badge>
        ) : null}
        {provider.builtin ? (
          <Badge variant="outline" className="text-[10px]">
            DSH 内置
          </Badge>
        ) : null}
      </div>

      <span className="mt-3 text-[11px] font-medium text-blue-600 opacity-0 transition-opacity group-hover:opacity-100">
        查看模型 →
      </span>
    </button>
  );
}

function ProviderDetail({
  provider,
  models,
  busy,
  onBack,
  onEdit,
  onToggle,
  onDelete,
  onAddModel,
  onEditModel,
  onActivateModel,
  onChangeReasoning,
  onToggleModel,
  onDeleteModel,
}: {
  provider: ProviderRow;
  models: ModelRow[];
  busy: boolean;
  onBack: () => void;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
  onAddModel: () => void;
  onEditModel: (model: ModelRow) => void;
  onActivateModel: (model: ModelRow) => void;
  onChangeReasoning: (model: ModelRow, value: string) => void;
  onToggleModel: (model: ModelRow) => void;
  onDeleteModel: (model: ModelRow) => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft className="h-4 w-4" />
          全部供应商
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={onEdit} disabled={busy}>
            <Pencil className="h-4 w-4" />
            编辑供应商
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onToggle}
            disabled={busy || (provider.enabled && Boolean(provider.active_model_id))}
            title={
              provider.enabled && provider.active_model_id
                ? "当前模型在此供应商下；先切换到别的模型再禁用"
                : provider.enabled
                  ? "禁用供应商"
                  : "启用供应商"
            }
          >
            <Power className="h-4 w-4" />
            {provider.enabled ? "禁用" : "启用"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-red-600 hover:bg-red-50 hover:text-red-700"
            onClick={onDelete}
            disabled={busy || provider.model_count > 0}
            title={
              provider.model_count > 0
                ? "先删除该供应商下的模型，再删除供应商"
                : "删除供应商"
            }
          >
            <Trash2 className="h-4 w-4" />
            删除
          </Button>
          <Button size="sm" onClick={onAddModel} disabled={busy}>
            <Plus className="h-4 w-4" />
            添加模型
          </Button>
        </div>
      </div>

      <SectionCard
        icon={Server}
        title={provider.display_name}
        description={`${provider.provider} · ${provider.api || "openai-completions"}`}
        badge={`${models.length} 个模型`}
      >
        <div className="grid gap-px bg-blue-50 sm:grid-cols-3">
          <Detail label="Base URL" mono>
            {provider.base_url || "-"}
          </Detail>
          <Detail label="Key 状态">
            <Badge
              variant={
                provider.api_key_source === "stored"
                  ? "success"
                  : provider.api_key_source === "env"
                    ? "secondary"
                    : "destructive"
              }
            >
              <KeyRound className="mr-1 h-3 w-3" />
              {provider.api_key_source === "stored"
                ? "已保存到数据库"
                : provider.api_key_source === "env"
                  ? "已注入"
                  : "未设置"}
            </Badge>
          </Detail>
          <Detail label="更新">
            {formatTs(provider.updated_at)}
          </Detail>
        </div>

        <div className="border-t border-blue-50 px-5 py-3 text-xs text-slate-500">
          {provider.api_key_source === "stored"
            ? "密钥保存在 models.db（文件权限 600），面板不回显；修改后机器人在下一轮消息自动重载。"
            : provider.api_key_source === "env"
              ? "密钥由运行环境注入；在“编辑供应商”里填写可改为数据库保存。"
              : "在“编辑供应商”里填写 API Key。"}
          {provider.builtin ? " 内置 provider 的协议由 DSH 固定。" : ""}
        </div>
      </SectionCard>

      {!provider.enabled ? (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
          <CircleSlash2 className="mt-0.5 h-4 w-4 shrink-0" />
          <span>该供应商已禁用，其模型不会写入 DSH route；启用后才能切换过去。</span>
        </div>
      ) : null}

      <SectionCard
        icon={Boxes}
        title="模型"
        badge={models.length}
        actions={
          <Button size="sm" variant="outline" onClick={onAddModel} disabled={busy}>
            <Plus className="h-4 w-4" />
            添加模型
          </Button>
        }
      >
        {models.length ? (
          <Table className="table-auto">
            <TableHeader>
              <TableRow>
                <TableHead>模型</TableHead>
                <TableHead className="text-right">上下文</TableHead>
                <TableHead>Reasoning</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.map((model) => {
                return (
                  <TableRow key={`${model.provider}/${model.model_id}`}>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium text-slate-800">{model.display_name}</span>
                        {model.is_active ? <Badge>当前</Badge> : null}
                        {!model.enabled ? <Badge variant="muted">禁用</Badge> : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {formatNumber(model.context_window)}
                    </TableCell>
                    <TableCell>
                      {(() => {
                        const declared = Array.isArray(
                          model.capabilities.reasoning_efforts
                        )
                          ? (model.capabilities.reasoning_efforts as unknown[])
                              .map((value) => String(value))
                              .filter(Boolean)
                          : [];
                        const options = Array.from(
                          new Set(
                            model.reasoning_effort
                              ? [...declared, model.reasoning_effort]
                              : declared
                          )
                        );
                        return (
                          <select
                            value={model.reasoning_effort}
                            onChange={(event) =>
                              onChangeReasoning(model, event.target.value)
                            }
                            disabled={busy || options.length === 0}
                            title={
                              options.length
                                ? "修改默认 reasoning_effort"
                                : "先在编辑里声明 Reasoning 档位"
                            }
                            className="h-8 rounded-md border border-blue-200 bg-white px-2 text-xs text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            <option value="">默认</option>
                            {options.map((effort) => (
                              <option key={effort} value={effort}>
                                {effort}
                              </option>
                            ))}
                          </select>
                        );
                      })()}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        {model.is_active ? null : (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => onActivateModel(model)}
                            disabled={
                              busy ||
                              !model.enabled ||
                              !provider.enabled ||
                              !provider.api_key_set
                            }
                            title={
                              !provider.enabled
                                ? "供应商已禁用"
                                : !provider.api_key_set
                                  ? "先设置 API Key"
                                  : !model.enabled
                                    ? "模型已禁用"
                                    : "设为当前模型"
                            }
                          >
                            <Zap className="h-3.5 w-3.5" />
                            设为当前
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => onEditModel(model)}
                          disabled={busy}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                          编辑
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => onToggleModel(model)}
                          disabled={busy || model.is_active}
                          title={model.is_active ? "当前模型不能禁用" : undefined}
                        >
                          <Power className="h-3.5 w-3.5" />
                          {model.enabled ? "禁用" : "启用"}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-red-600 hover:bg-red-50 hover:text-red-700"
                          onClick={() => onDeleteModel(model)}
                          disabled={busy || model.is_active}
                          title={model.is_active ? "当前模型不能删除" : "删除模型"}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          删除
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          <EmptyState
            icon={Boxes}
            text="这个供应商还没有模型。"
            hint="添加模型时填写模型 ID、上下文窗口和输出上限；连接信息继承供应商。"
          />
        )}
      </SectionCard>
    </div>
  );
}

function Detail({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="bg-white px-5 py-3">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <p
        className={cn(
          "mt-1 break-all text-sm text-slate-800",
          mono && "font-mono text-[11px]"
        )}
      >
        {children}
      </p>
    </div>
  );
}

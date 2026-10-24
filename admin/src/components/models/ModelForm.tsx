import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Gauge, RefreshCw, Save } from "lucide-react";

import { ErrorBanner } from "@/components/common/Bits";
import { CheckboxRow, Field, Modal, Select } from "@/components/models/Modal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ModelRow, ProviderRow } from "@/lib/analytics";
import { requestJson } from "@/lib/api";
import { cn } from "@/lib/utils";

export type ModelFormValues = {
  provider: string;
  model_id: string;
  display_name: string;
  context_window: string;
  max_output_tokens: string;
  reasoning_effort: string;
  capabilities: Record<string, unknown>;
  enabled: boolean;
};

const EFFORTS = ["low", "medium", "high"] as const;
type ReasoningMode = "none" | "low" | "medium" | "high" | "all" | "custom";

function declaredStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter((item) => item.length > 0);
}

function supportedEfforts(mode: ReasoningMode, custom: string[]): string[] {
  if (mode === "none") return [];
  if (mode === "all") return [...EFFORTS];
  if (mode === "custom") return custom;
  return [mode];
}

function isStandardEffort(value: string): value is (typeof EFFORTS)[number] {
  return (EFFORTS as readonly string[]).includes(value);
}

export function ModelForm({
  open,
  mode,
  provider,
  model,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean;
  mode: "create" | "edit";
  provider: ProviderRow;
  model?: ModelRow;
  busy: boolean;
  error: string;
  onClose: () => void;
  onSubmit: (values: ModelFormValues) => void;
}) {
  const [values, setValues] = useState({
    model_id: "",
    display_name: "",
    context_window: "",
    max_output_tokens: "2048",
  });
  const [reasoningMode, setReasoningMode] = useState<ReasoningMode>("none");
  const [customEfforts, setCustomEfforts] = useState<string[]>([]);
  const [imageInput, setImageInput] = useState(false);
  const [extras, setExtras] = useState<Record<string, unknown>>({});
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [modelsBusy, setModelsBusy] = useState(false);
  const [modelsError, setModelsError] = useState("");
  const [manualId, setManualId] = useState(false);
  const fetchAbort = useRef<AbortController | null>(null);
  const modelKey = model ? `${provider.provider}/${model.model_id}` : "";

  useEffect(() => {
    fetchAbort.current?.abort();
    fetchAbort.current = null;
    setModelsBusy(false);
    setModelOptions([]);
    setModelsError("");
    setManualId(false);
    if (!open) return;
    if (mode === "edit" && model) {
      setValues({
        model_id: model.model_id,
        display_name: model.display_name,
        context_window: String(model.context_window),
        max_output_tokens: String(model.max_output_tokens),
      });
      const declared = declaredStringList(model.capabilities.reasoning_efforts);
      const known = declared.filter(isStandardEffort);
      const extra = declared.filter((effort) => !isStandardEffort(effort));
      let nextMode: ReasoningMode = "none";
      if (declared.length) {
        if (extra.length) nextMode = "custom";
        else if (known.length === EFFORTS.length) nextMode = "all";
        else if (known.length === 1) nextMode = known[0];
        else nextMode = "custom";
      }
      setReasoningMode(nextMode);
      setCustomEfforts(nextMode === "custom" ? declared : []);
      setImageInput(declaredStringList(model.capabilities.input).includes("image"));
      setExtras(
        Object.fromEntries(
          Object.entries(model.capabilities).filter(
            ([key]) => key !== "reasoning_efforts" && key !== "input"
          )
        )
      );
      setReasoningEffort(
        declared.includes(model.reasoning_effort) ? model.reasoning_effort : ""
      );
      setEnabled(model.enabled);
    } else {
      setValues({
        model_id: "",
        display_name: "",
        context_window: "",
        max_output_tokens: "2048",
      });
      setReasoningMode("none");
      setCustomEfforts([]);
      setImageInput(false);
      setExtras({});
      setReasoningEffort("");
      setEnabled(true);
    }
    // Initialize on open / target change, not on every background refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, modelKey]);

  useEffect(() => () => fetchAbort.current?.abort(), []);

  const extraKeys = useMemo(() => Object.keys(extras), [extras]);
  const supported = supportedEfforts(reasoningMode, customEfforts);
  const canSubmit =
    !busy &&
    values.model_id.trim().length > 0 &&
    Number(values.context_window) > 0 &&
    Number(values.max_output_tokens) > 0;

  function changeReasoningMode(next: ReasoningMode) {
    setReasoningMode(next);
    const nextSupported = supportedEfforts(next, customEfforts);
    if (!nextSupported.length) {
      setReasoningEffort("");
      return;
    }
    if (!nextSupported.includes(reasoningEffort)) {
      setReasoningEffort(next === "all" ? "low" : nextSupported[0]);
    }
  }

  async function fetchModels() {
    fetchAbort.current?.abort();
    const controller = new AbortController();
    fetchAbort.current = controller;
    setModelsBusy(true);
    setModelsError("");
    try {
      const payload = await requestJson<{ models: Array<{ id: string }> }>(
        "/api/models/provider-models",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: provider.provider }),
          signal: controller.signal,
        }
      );
      if (controller.signal.aborted) return;
      const ids = payload.models
        .map((item) => item.id)
        .filter((id) => id.length > 0);
      setModelOptions(ids);
      setManualId(false);
      if (ids.length) {
        setValues((current) =>
          current.model_id && ids.includes(current.model_id)
            ? current
            : { ...current, model_id: ids[0] }
        );
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      setModelsError(
        err instanceof Error && err.message ? err.message : "获取模型列表失败"
      );
    } finally {
      if (fetchAbort.current === controller) {
        fetchAbort.current = null;
        setModelsBusy(false);
      }
    }
  }

  function startManual() {
    fetchAbort.current?.abort();
    fetchAbort.current = null;
    setModelsBusy(false);
    setManualId(true);
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    const capabilities: Record<string, unknown> = { ...extras };
    if (supported.length) capabilities.reasoning_efforts = supported;
    capabilities.input = imageInput ? ["text", "image"] : ["text"];
    onSubmit({
      provider: provider.provider,
      model_id: values.model_id.trim(),
      display_name: values.display_name.trim() || values.model_id.trim(),
      context_window: values.context_window.trim(),
      max_output_tokens: values.max_output_tokens.trim(),
      reasoning_effort: supported.length ? reasoningEffort : "",
      capabilities,
      enabled,
    });
  }

  return (
    <Modal
      open={open}
      title={mode === "edit" ? "编辑模型" : `添加模型 · ${provider.display_name}`}
      description={`模型挂在供应商 ${provider.provider} 下，连接信息跟随供应商。`}
      onClose={onClose}
      wide
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button type="submit" form="model-form" disabled={!canSubmit}>
            <Save className="h-4 w-4" />
            {busy ? "保存中…" : mode === "edit" ? "保存修改" : "添加模型"}
          </Button>
        </>
      }
    >
      <form id="model-form" className="space-y-4" onSubmit={handleSubmit}>
        {error ? <ErrorBanner message={error} /> : null}

        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/50 px-3 py-2 text-[11px] text-slate-500">
          <span className="font-mono">{provider.base_url || "（未配置 base_url）"}</span>
          <Badge variant="outline">{provider.api || "openai-completions"}</Badge>
          <Badge variant={provider.enabled ? "success" : "muted"}>
            {provider.enabled ? "供应商已启用" : "供应商已禁用"}
          </Badge>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <label
                htmlFor="model-id"
                className="text-xs font-medium text-slate-600"
              >
                模型 ID
              </label>
              {mode === "create" ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void fetchModels()}
                  disabled={modelsBusy}
                >
                  <RefreshCw className={cn("h-3.5 w-3.5", modelsBusy && "animate-spin")} />
                  {modelsBusy ? "获取中…" : "获取模型"}
                </Button>
              ) : null}
            </div>
            {modelOptions.length && !manualId ? (
              <Select
                id="model-id"
                value={values.model_id}
                onChange={(value) =>
                  setValues((current) => ({ ...current, model_id: value }))
                }
              >
                {modelOptions.map((modelId) => (
                  <option key={modelId} value={modelId}>
                    {modelId}
                  </option>
                ))}
              </Select>
            ) : (
              <Input
                id="model-id"
                value={values.model_id}
                onChange={(event) =>
                  setValues((current) => ({ ...current, model_id: event.target.value }))
                }
                disabled={mode === "edit"}
                placeholder={mode === "edit" ? values.model_id : "点击“获取模型”或手动输入"}
                spellCheck={false}
              />
            )}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-[11px] text-slate-400">
                {modelOptions.length && !manualId
                  ? `已从供应商拉取 ${modelOptions.length} 个模型。`
                  : "发给上游的 model 字段；创建后不可修改。"}
              </p>
              {modelOptions.length && !manualId && mode === "create" ? (
                <button
                  type="button"
                  onClick={startManual}
                  className="text-[11px] text-blue-600 hover:underline"
                >
                  手动填写
                </button>
              ) : null}
            </div>
            {modelsError ? (
              <p className="rounded-lg border border-red-100 bg-red-50 px-2 py-1 text-[11px] text-red-700">
                {modelsError}
              </p>
            ) : null}
          </div>
          <Field label="显示名称" hint="留空时使用模型 ID。">
            <Input
              value={values.display_name}
              onChange={(event) =>
                setValues((current) => ({ ...current, display_name: event.target.value }))
              }
              placeholder="DeepSeek Chat"
            />
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="上下文窗口" hint="tokens；DSH 的 token meter 和自动压缩用它。">
            <Input
              type="number"
              min={1}
              value={values.context_window}
              onChange={(event) =>
                setValues((current) => ({ ...current, context_window: event.target.value }))
              }
              placeholder="128000"
            />
          </Field>
          <Field label="最大输出 tokens" hint="单次请求的 max_tokens，建议 2048–8192。">
            <Input
              type="number"
              min={1}
              value={values.max_output_tokens}
              onChange={(event) =>
                setValues((current) => ({ ...current, max_output_tokens: event.target.value }))
              }
              placeholder="2048"
            />
          </Field>
        </div>

        <div className="space-y-3 rounded-xl border border-blue-100 p-3">
          <div className="flex items-center gap-2">
            <Gauge className="h-4 w-4 text-blue-500" />
            <span className="text-xs font-medium text-slate-600">能力与推理</span>
            {extraKeys.length ? (
              <span className="ml-auto flex flex-wrap gap-1">
                {extraKeys.map((key) => (
                  <Badge key={key} variant="outline" className="text-[10px]">
                    {key}
                  </Badge>
                ))}
              </span>
            ) : null}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Reasoning 档位"
              hint="声明模型支持的档位；不确定就选“不声明”。"
            >
              <Select
                value={reasoningMode}
                onChange={(value) => changeReasoningMode(value as ReasoningMode)}
              >
                <option value="none">不声明（模型默认）</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="all">low / medium / high</option>
                {customEfforts.length ? (
                  <option value="custom">自定义（保留原设置）</option>
                ) : null}
              </Select>
            </Field>
            <Field label="默认推理强度" hint="留空表示不发送 reasoning_effort，用模型自己的默认值。">
              <Select
                value={reasoningEffort}
                onChange={setReasoningEffort}
                disabled={!supported.length}
              >
                <option value="">模型默认</option>
                {supported.map((effort) => (
                  <option key={effort} value={effort}>
                    {effort}
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          {customEfforts.length && reasoningMode === "custom" ? (
            <p className="text-[11px] text-slate-400">
              保留已有自定义档位：{customEfforts.join(", ")}
            </p>
          ) : null}

          <CheckboxRow
            label="支持图片输入"
            hint="同时启用 DSH 的 read_image 工具"
            checked={imageInput}
            onChange={setImageInput}
          />
        </div>

        <label className="flex items-center gap-2.5 rounded-lg border border-blue-100 px-3 py-2.5 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={enabled}
            disabled={model?.is_active}
            onChange={(event) => setEnabled(event.target.checked)}
            className="h-4 w-4 rounded border-blue-300 accent-blue-600"
          />
          启用该模型
          {model?.is_active ? (
            <span className="ml-auto text-[11px] text-slate-400">当前模型不能禁用</span>
          ) : null}
        </label>
      </form>
    </Modal>
  );
}

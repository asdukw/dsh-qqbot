import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Gauge, RefreshCw, Save } from "lucide-react";

import { Notice } from "@/components/common/Bits";
import { Modal } from "@/components/models/Modal";
import { CheckboxRow, Field, FormSelect } from "@/components/ui/form";
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

const EFFORTS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
] as const;

function declaredStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter((item) => item.length > 0);
}

function isKnownEffort(value: string): boolean {
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
    context_window: "1000000",
  });
  const [imageInput, setImageInput] = useState(true);
  const [extras, setExtras] = useState<Record<string, unknown>>({});
  const [reasoningEffort, setReasoningEffort] = useState("none");
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
        context_window: String(model.context_window),
      });
      const declaredInput = declaredStringList(model.capabilities.input);
      setImageInput(declaredInput.length ? declaredInput.includes("image") : true);
      setExtras(
        Object.fromEntries(
          Object.entries(model.capabilities).filter(
            ([key]) => key !== "reasoning_efforts" && key !== "input"
          )
        )
      );
      setReasoningEffort(model.reasoning_effort || "none");
      setEnabled(model.enabled);
    } else {
      setValues({
        model_id: "",
        context_window: "1000000",
      });
      setImageInput(true);
      setExtras({});
      setReasoningEffort("none");
      setEnabled(true);
    }
    // Initialize on open / target change, not on every background refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, modelKey]);

  useEffect(() => () => fetchAbort.current?.abort(), []);

  const extraKeys = useMemo(() => Object.keys(extras), [extras]);
  const effortOptions = useMemo(
    () =>
      reasoningEffort && !isKnownEffort(reasoningEffort)
        ? [reasoningEffort, ...EFFORTS]
        : [...EFFORTS],
    [reasoningEffort]
  );
  const canSubmit = !busy && values.model_id.trim().length > 0;

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
    const contextWindow = Number(values.context_window);
    const contextWindowValue =
      Number.isFinite(contextWindow) && contextWindow > 0
        ? values.context_window.trim()
        : "1000000";
    const declaredEfforts =
      reasoningEffort && !isKnownEffort(reasoningEffort)
        ? [reasoningEffort, ...EFFORTS]
        : [...EFFORTS];
    const capabilities: Record<string, unknown> = {
      ...extras,
      reasoning_efforts: declaredEfforts,
    };
    capabilities.input = imageInput ? ["text", "image"] : ["text"];
    onSubmit({
      provider: provider.provider,
      model_id: values.model_id.trim(),
      display_name:
        mode === "edit" && model?.display_name
          ? model.display_name
          : values.model_id.trim(),
      context_window: contextWindowValue,
      max_output_tokens: "0",
      reasoning_effort: reasoningEffort === "none" ? "" : reasoningEffort,
      capabilities,
      enabled,
    });
  }

  return (
    <Modal
      open={open}
      title={mode === "edit" ? "编辑模型" : `添加模型 · ${provider.display_name}`}
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
        {error ? <Notice tone="danger">{error}</Notice> : null}

        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
          <span className="font-mono">{provider.base_url || "（未配置 base_url）"}</span>
          <Badge variant="outline">{provider.api || "openai-completions"}</Badge>
          <Badge variant={provider.enabled ? "success" : "muted"}>
            {provider.enabled ? "供应商已启用" : "供应商已禁用"}
          </Badge>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <label htmlFor="model-id" className="text-xs font-medium text-slate-600">
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
            <FormSelect
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
            </FormSelect>
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
            <p className="text-xs text-slate-500">
              {modelOptions.length && !manualId
                ? `已从供应商拉取 ${modelOptions.length} 个模型。`
                : "创建后不可修改。"}
            </p>
            {modelOptions.length && !manualId && mode === "create" ? (
              <button
                type="button"
                onClick={startManual}
                className="text-xs text-blue-600 hover:underline"
              >
                手动填写
              </button>
            ) : null}
          </div>
          {modelsError ? <Notice tone="danger">{modelsError}</Notice> : null}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            className="sm:col-span-2"
            label="上下文窗口"
            hint="tokens；留空默认 1000000。"
          >
            <Input
              type="number"
              min={1}
              value={values.context_window}
              onChange={(event) =>
                setValues((current) => ({ ...current, context_window: event.target.value }))
              }
              placeholder="1000000"
            />
          </Field>
        </div>

        <div className="space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
          <div className="flex items-center gap-2">
            <Gauge className="h-4 w-4 text-slate-500" />
            <span className="text-sm font-semibold text-slate-900">能力与推理</span>
            {extraKeys.length ? (
              <span className="ml-auto flex flex-wrap gap-1">
                {extraKeys.map((key) => (
                  <Badge key={key} variant="outline">
                    {key}
                  </Badge>
                ))}
              </span>
            ) : null}
          </div>

          <Field label="默认推理强度" hint="选 none 表示不发送该参数。">
            <FormSelect value={reasoningEffort} onChange={setReasoningEffort}>
              {effortOptions.map((effort) => (
                <option key={effort} value={effort}>
                  {effort}
                </option>
              ))}
            </FormSelect>
          </Field>
        </div>

        <CheckboxRow
          label="启用该模型"
          hint={model?.is_active ? "当前模型不能禁用" : undefined}
          checked={enabled}
          disabled={model?.is_active}
          onChange={setEnabled}
        />
      </form>
    </Modal>
  );
}

import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { KeyRound, Save, ShieldAlert, Wand2 } from "lucide-react";

import { ErrorBanner } from "@/components/common/Bits";
import { Field, Modal } from "@/components/models/Modal";
import { ProviderIcon } from "@/components/models/ProviderIcon";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ProviderPreset, ProviderRow } from "@/lib/analytics";
import { cn } from "@/lib/utils";

export type ProviderFormValues = {
  provider: string;
  display_name: string;
  api: string;
  base_url: string;
  api_key?: string;
  enabled: boolean;
};

const BUILTIN_PROVIDERS = new Set(["opencode", "opencode-go"]);

const EMPTY: ProviderFormValues = {
  provider: "",
  display_name: "",
  api: "openai-completions",
  base_url: "",
  api_key: "",
  enabled: true,
};

const KEY_SOURCE_LABEL = {
  stored: "已保存到数据库",
  env: "已注入",
  missing: "未设置",
} as const;

export function ProviderForm({
  open,
  mode,
  provider,
  presets,
  existingProviders,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean;
  mode: "create" | "edit";
  provider?: ProviderRow;
  presets: ProviderPreset[];
  existingProviders: string[];
  busy: boolean;
  error: string;
  onClose: () => void;
  onSubmit: (values: ProviderFormValues) => void;
}) {
  const [values, setValues] = useState<ProviderFormValues>(EMPTY);
  const [presetId, setPresetId] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const providerId = provider?.provider ?? "";

  useEffect(() => {
    if (!open) return;
    setPresetId("");
    setClearKey(false);
    if (mode === "edit" && provider) {
      setValues({
        provider: provider.provider,
        display_name: provider.display_name,
        api: provider.api || "openai-completions",
        base_url: provider.base_url,
        api_key: "",
        enabled: provider.enabled,
      });
    } else {
      setValues(EMPTY);
    }
    // Re-initialize only when the dialog opens for a particular provider.
    // The provider object is replaced by every 60s refresh; depending on it
    // directly would wipe the form while the user is typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, providerId]);

  const availablePresets = useMemo(
    () =>
      presets.filter(
        (preset) => mode === "edit" || !existingProviders.includes(preset.provider)
      ),
    [presets, existingProviders, mode]
  );

  const isBuiltin = BUILTIN_PROVIDERS.has(values.provider.trim());
  const keySource = provider?.api_key_source ?? "missing";
  const canSubmit =
    !busy &&
    /^[a-z0-9][a-z0-9._-]{0,63}$/.test(values.provider.trim()) &&
    values.base_url.trim().length > 0 &&
    values.display_name.trim().length > 0 &&
    (mode === "edit" || (values.api_key ?? "").trim().length > 0);

  function update<K extends keyof ProviderFormValues>(key: K, value: ProviderFormValues[K]) {
    setValues((current) => ({ ...current, [key]: value }));
  }

  function applyPreset(id: string) {
    setPresetId(id);
    const preset = presets.find((item) => item.provider === id);
    if (!preset) {
      setValues(EMPTY);
      return;
    }
    setValues((current) => ({
      ...current,
      provider: preset.provider,
      display_name: preset.display_name,
      api: preset.api,
      base_url: preset.base_url,
      api_key: "",
      enabled: true,
    }));
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    const payload: ProviderFormValues = {
      ...values,
      provider: values.provider.trim(),
      display_name: values.display_name.trim(),
      api: values.api.trim() || "openai-completions",
      base_url: values.base_url.trim(),
    };
    const typedKey = (values.api_key ?? "").trim();
    if (mode === "create") {
      if (typedKey) payload.api_key = typedKey;
      else delete payload.api_key;
    } else if (clearKey) {
      payload.api_key = "";
    } else if (typedKey) {
      payload.api_key = typedKey;
    } else {
      delete payload.api_key;
    }
    onSubmit(payload);
  }

  return (
    <Modal
      open={open}
      title={mode === "edit" ? "编辑供应商" : "添加供应商"}
      description="供应商保存协议、端点和密钥；同一供应商下的模型共享这些连接信息。"
      onClose={onClose}
      wide
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button type="submit" form="provider-form" disabled={!canSubmit}>
            <Save className="h-4 w-4" />
            {busy ? "保存中…" : mode === "edit" ? "保存修改" : "添加供应商"}
          </Button>
        </>
      }
    >
      <form id="provider-form" className="space-y-4" onSubmit={handleSubmit}>
        {error ? <ErrorBanner message={error} /> : null}

        {mode === "create" ? (
          <div className="space-y-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs font-medium text-slate-600">从预设开始</span>
              <span className="text-[11px] text-slate-400">预设只填充表单，保存后仍可修改</span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {availablePresets.map((preset) => {
                const selected = presetId === preset.provider;
                return (
                  <button
                    key={preset.provider}
                    type="button"
                    onClick={() => applyPreset(preset.provider)}
                    className={cn(
                      "flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors",
                      selected
                        ? "border-blue-400 bg-blue-50 ring-1 ring-blue-300"
                        : "border-blue-100 bg-white hover:border-blue-300 hover:bg-blue-50/50"
                    )}
                  >
                    <ProviderIcon provider={preset.provider} className="h-9 w-9" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-slate-800">
                        {preset.display_name}
                      </span>
                      <span className="block truncate font-mono text-[10px] text-slate-400">
                        {preset.provider}
                      </span>
                    </span>
                  </button>
                );
              })}
              <button
                type="button"
                onClick={() => applyPreset("")}
                className={cn(
                  "flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors",
                  presetId === ""
                    ? "border-blue-400 bg-blue-50 ring-1 ring-blue-300"
                    : "border-blue-100 bg-white hover:border-blue-300 hover:bg-blue-50/50"
                )}
              >
                <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-slate-100 text-slate-500">
                  <Wand2 className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-slate-800">手动填写</span>
                  <span className="block text-[10px] text-slate-400">自定义 route 与端点</span>
                </span>
              </button>
            </div>
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="供应商 ID"
            hint="DSH route 名，小写字母/数字/._-；创建后不可修改。"
          >
            <Input
              value={values.provider}
              onChange={(event) => update("provider", event.target.value.toLowerCase())}
              disabled={mode === "edit"}
              placeholder="deepseek"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="显示名称" hint="面板和 DSH route 上显示的名字。">
            <Input
              value={values.display_name}
              onChange={(event) => update("display_name", event.target.value)}
              placeholder="DeepSeek"
            />
          </Field>
        </div>

        <Field label="Base URL" hint="OpenAI 兼容端点，通常以 /v1 结尾；不要带 /chat/completions。">
          <Input
            value={values.base_url}
            onChange={(event) => update("base_url", event.target.value)}
            placeholder="https://api.deepseek.com"
            spellCheck={false}
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="API 协议"
            hint={
              isBuiltin
                ? "内置 provider 由 DSH 提供 adapter，协议不可覆盖。"
                : "对应 llm-pi-ai 的 adapter，默认 openai-completions。"
            }
            className="sm:col-span-2"
          >
            <Input
              value={values.api}
              onChange={(event) => update("api", event.target.value)}
              disabled={isBuiltin}
              placeholder="openai-completions"
              spellCheck={false}
            />
          </Field>
        </div>

        <div className="space-y-2 rounded-xl border border-blue-100 bg-blue-50/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
              <KeyRound className="h-3.5 w-3.5 text-blue-500" />
              API Key
            </span>
            {mode === "edit" ? (
              <Badge variant={keySource === "missing" ? "muted" : "success"}>
                {KEY_SOURCE_LABEL[keySource]}
              </Badge>
            ) : null}
          </div>
          <Input
            type="password"
            value={values.api_key ?? ""}
            onChange={(event) => update("api_key", event.target.value)}
            disabled={clearKey}
            placeholder={
              mode === "create"
                ? "sk-...（必填）"
                : keySource === "missing"
                  ? "sk-...（必填）"
                  : "已注入，留空保持不变"
            }
            autoComplete="new-password"
            spellCheck={false}
          />
          <p className="text-[11px] leading-relaxed text-slate-500">
            直接填写会保存到 <code className="rounded bg-white px-1 py-0.5 font-mono">models.db</code>
            （文件权限 600），面板不会回显已保存的值。创建供应商时必须填写；编辑时留空保持不变。
          </p>
          {mode === "edit" && keySource === "stored" ? (
            <label className="flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={clearKey}
                onChange={(event) => setClearKey(event.target.checked)}
                className="h-3.5 w-3.5 rounded border-blue-300 accent-blue-600"
              />
              清除已保存的密钥
            </label>
          ) : null}
        </div>

        <label className="flex items-center gap-2.5 rounded-lg border border-blue-100 px-3 py-2.5 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={values.enabled}
            onChange={(event) => update("enabled", event.target.checked)}
            className="h-4 w-4 rounded border-blue-300 accent-blue-600"
          />
          启用该供应商
          {provider?.active_model_id ? (
            <Badge variant="muted" className="ml-auto">
              <ShieldAlert className="mr-1 h-3 w-3" /> 当前模型在此供应商下
            </Badge>
          ) : null}
        </label>

        {isBuiltin ? (
          <p className="text-[11px] leading-relaxed text-amber-600">
            内置 provider 使用 DSH 自带的 adapter（OpenCode 会按会话自动加{" "}
            <code>x-opencode-session</code>）。连接信息可以改，协议字段会被忽略。
          </p>
        ) : null}
      </form>
    </Modal>
  );
}

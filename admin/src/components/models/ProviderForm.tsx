import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { KeyRound, Save, Wand2 } from "lucide-react";

import { Notice } from "@/components/common/Bits";
import { Modal } from "@/components/models/Modal";
import { CheckboxRow, Field } from "@/components/ui/form";
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
const PROVIDER_ID_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

// Derive a readable id from a Latin display name; Chinese names fall back to
// "provider" and the uniqueness suffix.
function slugifyProvider(value: string): string {
  const slug = value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[._-]+|[._-]+$/g, "")
    .slice(0, 48);
  return PROVIDER_ID_RE.test(slug) ? slug : "";
}

function uniqueProviderId(base: string, existing: string[]): string {
  const reserved = new Set(existing.map((value) => value.toLowerCase()));
  const root = base || "provider";
  if (!reserved.has(root)) return root;
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${root}-${index}`.slice(0, 64);
    if (!reserved.has(candidate)) return candidate;
  }
  return `${root}-${Math.random().toString(36).slice(2, 6)}`.slice(0, 64);
}

const EMPTY: ProviderFormValues = {
  provider: "",
  display_name: "",
  api: "openai-completions",
  base_url: "",
  api_key: "",
  enabled: true,
};

const KEY_SOURCE_LABEL = {
  stored: "Key 已保存",
  env: "Key 来自环境",
  missing: "Key 未设置",
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
  const previewId =
    values.provider.trim() ||
    uniqueProviderId(slugifyProvider(values.display_name), existingProviders);
  const canSubmit =
    !busy &&
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
    const providerId =
      values.provider.trim() ||
      uniqueProviderId(slugifyProvider(values.display_name), existingProviders);
    const payload: ProviderFormValues = {
      ...values,
      provider: providerId,
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
        {error ? <Notice tone="danger">{error}</Notice> : null}

        {mode === "create" ? (
          <div className="space-y-2">
            <p className="text-xs font-medium text-slate-600">从预设开始</p>
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
                        ? "border-blue-500 bg-blue-50 ring-1 ring-blue-500"
                        : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
                    )}
                  >
                    <ProviderIcon provider={preset.provider} className="h-9 w-9" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-slate-800">
                        {preset.display_name}
                      </span>
                      <span className="block truncate font-mono text-xs text-slate-500">
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
                    ? "border-blue-500 bg-blue-50 ring-1 ring-blue-500"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
                )}
              >
                <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-slate-100 text-slate-500">
                  <Wand2 className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-slate-800">手动填写</span>
                  <span className="block text-xs text-slate-500">自定义端点</span>
                </span>
              </button>
            </div>
          </div>
        ) : null}

        <Field label="显示名称" hint="面板中显示的名称。">
          <Input
            value={values.display_name}
            onChange={(event) => update("display_name", event.target.value)}
            placeholder="DeepSeek"
          />
        </Field>
        <p className="-mt-2 text-xs text-slate-500">
          {mode === "edit"
            ? `供应商 ID：${values.provider}`
            : values.display_name.trim()
              ? `保存时自动生成供应商 ID：${previewId}`
              : "保存时自动生成供应商 ID"}
        </p>

        <Field label="Base URL" hint="OpenAI 兼容端点，通常以 /v1 结尾；不要带 /chat/completions。">
          <Input
            value={values.base_url}
            onChange={(event) => update("base_url", event.target.value)}
            placeholder="https://api.deepseek.com"
            spellCheck={false}
          />
        </Field>

        <Field
          label="API 协议"
          hint={isBuiltin ? "内置 provider 协议不可覆盖。" : "默认 openai-completions。"}
        >
          <Input
            value={values.api}
            onChange={(event) => update("api", event.target.value)}
            disabled={isBuiltin}
            placeholder="openai-completions"
            spellCheck={false}
          />
        </Field>

        <div className="space-y-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
              <KeyRound className="h-3.5 w-3.5 text-slate-500" />
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
            aria-label="API Key"
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
          <p className="text-xs text-slate-500">密钥保存在服务端，面板不回显。</p>
          {mode === "edit" && keySource === "stored" ? (
            <CheckboxRow
              label="清除已保存的密钥"
              checked={clearKey}
              onChange={setClearKey}
              className="border-slate-200 bg-white"
            />
          ) : null}
        </div>

        <CheckboxRow
          label="启用该供应商"
          hint={provider?.active_model_id ? "当前模型在此供应商下" : undefined}
          checked={values.enabled}
          onChange={(checked) => update("enabled", checked)}
        />

        {isBuiltin ? (
          <Notice tone="warning">
            内置 provider 的协议字段会被忽略，连接信息仍可修改。
          </Notice>
        ) : null}
      </form>
    </Modal>
  );
}

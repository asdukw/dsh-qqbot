import { useRef, useState } from "react";
import { KeyRound } from "lucide-react";

import {
  ErrorBanner,
  LoadingHint,
  Notice,
  PageStack,
  SectionCard,
} from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, FormSelect } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import type { VideoCredentialsData, VideoCredentialRow } from "@/lib/analytics";
import { requestJson } from "@/lib/api";
import { useApi } from "@/lib/useApi";

const LABELS: Record<string, string> = { bilibili: "哔哩哔哩", douyin: "抖音（实验性）" };
const SOURCES: Record<string, string> = {
  managed: "面板保存",
  legacy: "运维文件",
  cleared: "已明确清除",
  missing: "未配置",
};

const TEXTAREA_CLASS =
  "block w-full rounded-lg border border-slate-200 bg-white p-3 font-mono text-xs text-slate-900 shadow-sm placeholder:text-slate-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";

function PlatformForm({ row, canWrite, onSaved }: { row: VideoCredentialRow; canWrite: boolean; onSaved: () => void }) {
  const [format, setFormat] = useState<"header" | "netscape">("header");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const local =
    ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname) &&
    window.location.protocol === "http:";
  const writable = canWrite && local;
  const clearDraft = () => {
    setContent("");
    if (file.current) file.current.value = "";
  };

  async function submit(remove: boolean) {
    if (!writable || busy) return;
    if (remove && !window.confirm(`清除${LABELS[row.platform]}凭据？后续请求将匿名访问，不会恢复旧运维 Cookie。`)) return;
    if (!remove && new TextEncoder().encode(content).length > 65536) {
      setError("Cookie 内容不能超过 64 KiB");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await requestJson<VideoCredentialsData>(`/api/video/credentials${remove ? "/delete" : ""}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(remove ? { platform: row.platform } : { platform: row.platform, format, content }),
      });
      clearDraft();
      onSaved();
      setNotice(remove ? "已清除；后续操作匿名访问。" : "已保存；后续解析自动使用新凭据。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <SectionCard
      icon={KeyRound}
      title={LABELS[row.platform]}
      description={`来源：${SOURCES[row.source]}${
        row.updated_at ? ` · 更新于 ${new Date(row.updated_at * 1000).toLocaleString()}` : ""
      }`}
      actions={
        <>
          <Badge variant={row.configured ? "success" : "muted"}>
            {row.configured ? "已配置" : "未配置"}
          </Badge>
          <Badge variant={row.enabled ? "secondary" : "outline"}>
            平台{row.enabled ? "已启用" : "未启用"}
          </Badge>
        </>
      }
      contentClassName="space-y-4 p-5"
    >
      {error ? <ErrorBanner message={error} /> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <Field label="输入格式" htmlFor={`format-${row.platform}`}>
        <FormSelect
          id={`format-${row.platform}`}
          disabled={!writable || busy}
          value={format}
          onChange={(value) => {
            setFormat(value as "header" | "netscape");
            clearDraft();
          }}
        >
          <option value="header">Cookie 请求头</option>
          <option value="netscape">Netscape Cookie 文件</option>
        </FormSelect>
      </Field>

      <Field label="替换凭据" hint="不会显示已保存的值" htmlFor={`content-${row.platform}`}>
        {format === "header" ? (
          <Input
            id={`content-${row.platform}`}
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={content}
            disabled={!writable || busy}
            placeholder="name=value; other=value"
            onChange={(event) => setContent(event.target.value)}
          />
        ) : (
          <textarea
            id={`content-${row.platform}`}
            autoComplete="off"
            spellCheck={false}
            value={content}
            disabled={!writable || busy}
            rows={5}
            className={TEXTAREA_CLASS}
            placeholder="# Netscape HTTP Cookie File"
            onChange={(event) => setContent(event.target.value)}
          />
        )}
      </Field>

      {format === "netscape" ? (
        <Field label="从文件导入" htmlFor={`file-${row.platform}`}>
          <Input
            id={`file-${row.platform}`}
            ref={file}
            type="file"
            accept=".txt,text/plain"
            disabled={!writable || busy}
            onChange={async (event) => {
              const selected = event.target.files?.[0];
              if (!selected) return;
              if (selected.size > 65536) {
                setError("文件不能超过 64 KiB");
                clearDraft();
                return;
              }
              try {
                setContent(await selected.text());
                setError("");
              } catch {
                setError("无法读取文件");
              }
            }}
          />
        </Field>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button disabled={!writable || busy || !content.trim()} onClick={() => void submit(false)}>
          {busy ? "处理中…" : "保存并替换"}
        </Button>
        <Button variant="outline" disabled={!writable || busy} onClick={() => void submit(true)}>
          清除凭据
        </Button>
      </div>
      <p className="text-xs text-slate-500">保存不会自动启用平台。</p>
    </SectionCard>
  );
}

export default function VideoCredentialsPage({ refreshKey }: { refreshKey: number }) {
  const { data, loading, error, reload } = useApi<VideoCredentialsData>(
    "/api/video/credentials",
    refreshKey
  );

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      <p className="text-sm text-slate-600">
        Cookie 是账号凭据，仅保存在服务端，请勿发到聊天；已保存不代表上游登录有效。
      </p>
      <LoadingHint loading={loading && !data} />

      {data && !data.can_write ? (
        <Notice tone="warning">
          当前通道禁止修改凭据。请通过 SSH/Workbench 转发，在 http://127.0.0.1:本地端口 打开面板；不支持公网
          HTTP 或仅伪装为 HTTPS 的代理头。
        </Notice>
      ) : null}

      <div className="grid gap-5 xl:grid-cols-2">
        {data?.platforms.map((row) => (
          <PlatformForm key={row.platform} row={row} canWrite={data.can_write} onSaved={reload} />
        ))}
      </div>
    </PageStack>
  );
}

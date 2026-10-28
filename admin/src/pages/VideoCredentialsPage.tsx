import { useRef, useState } from "react";
import { KeyRound } from "lucide-react";
import { ErrorBanner } from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { VideoCredentialsData, VideoCredentialRow } from "@/lib/analytics";
import { requestJson } from "@/lib/api";
import { useApi } from "@/lib/useApi";

const LABELS: Record<string, string> = { bilibili: "哔哩哔哩", douyin: "抖音（实验性）" };
const SOURCES: Record<string, string> = { managed: "面板保存", legacy: "运维文件", cleared: "已明确清除", missing: "未配置" };

function PlatformForm({ row, canWrite, onSaved }: { row: VideoCredentialRow; canWrite: boolean; onSaved: () => void }) {
  const [format, setFormat] = useState<"header" | "netscape">("header");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname) && window.location.protocol === "http:";
  const writable = canWrite && local;
  const clearDraft = () => { setContent(""); if (file.current) file.current.value = ""; };

  async function submit(remove: boolean) {
    if (!writable || busy) return;
    if (remove && !window.confirm(`清除${LABELS[row.platform]}凭据？后续请求将匿名访问，不会恢复旧运维 Cookie。`)) return;
    if (!remove && new TextEncoder().encode(content).length > 65536) {
      setError("Cookie 内容不能超过 64 KiB"); return;
    }
    setBusy(true); setError(""); setNotice("");
    try {
      await requestJson<VideoCredentialsData>(`/api/video/credentials${remove ? "/delete" : ""}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(remove ? { platform: row.platform } : { platform: row.platform, format, content }),
      });
      clearDraft(); onSaved();
      setNotice(remove ? "已清除；后续操作匿名访问。" : "已保存；后续解析自动使用新凭据，无需重启。未验证上游登录状态。");
    } catch (err) { setError(err instanceof Error ? err.message : "操作失败"); }
    finally { setBusy(false); }
  }

  return <Card>
    <CardHeader><CardTitle className="flex items-center gap-2 text-base"><KeyRound className="h-4 w-4" />{LABELS[row.platform]}</CardTitle>
      <CardDescription>状态：{row.configured ? "已配置（未验证）" : "未配置"} · {SOURCES[row.source]} · 平台{row.enabled ? "已启用" : "未启用"}</CardDescription>
    </CardHeader>
    <CardContent className="space-y-4">
      <p className="text-xs text-muted-foreground">更新时间：{row.updated_at ? new Date(row.updated_at * 1000).toLocaleString() : "—"}。保存不会自动开启平台功能。</p>
      {error && <ErrorBanner message={error} />}
      {notice && <p role="status" className="text-sm text-emerald-700">{notice}</p>}
      <label className="block space-y-1 text-sm">输入格式
        <select disabled={!writable || busy} value={format} className="block w-full rounded-lg border p-2" onChange={e => { setFormat(e.target.value as "header" | "netscape"); clearDraft(); }}>
          <option value="header">Cookie 请求头</option><option value="netscape">Netscape Cookie 文件</option>
        </select>
      </label>
      <label className="block space-y-1 text-sm">替换凭据（不会显示已保存的值）
        {format === "header" ? <Input type="password" autoComplete="off" spellCheck={false} value={content} disabled={!writable || busy}
          placeholder="name=value; other=value" onChange={e => setContent(e.target.value)} /> :
          <textarea autoComplete="off" spellCheck={false} value={content} disabled={!writable || busy} rows={5}
            className="block w-full rounded-lg border p-2 font-mono text-xs" placeholder="# Netscape HTTP Cookie File" onChange={e => setContent(e.target.value)} />}
      </label>
      {format === "netscape" && <Input ref={file} type="file" accept=".txt,text/plain" disabled={!writable || busy} onChange={async e => {
        const selected = e.target.files?.[0];
        if (!selected) return;
        if (selected.size > 65536) { setError("文件不能超过 64 KiB"); clearDraft(); return; }
        try { setContent(await selected.text()); setError(""); } catch { setError("无法读取文件"); }
      }} />}
      <div className="flex gap-2"><Button disabled={!writable || busy || !content.trim()} onClick={() => void submit(false)}>{busy ? "处理中…" : "保存并替换"}</Button>
        <Button variant="outline" disabled={!writable || busy} onClick={() => void submit(true)}>清除凭据</Button></div>
      <p className="text-xs leading-relaxed text-muted-foreground">仅保存本平台 Cookie，最多 64 KiB。不读取浏览器、不自动登录，不写入浏览器存储。离开页面即丢弃未保存输入；清除不能撤回在途请求已读取的凭据。</p>
    </CardContent>
  </Card>;
}

export default function VideoCredentialsPage({ refreshKey }: { refreshKey: number }) {
  const { data, loading, error, reload } = useApi<VideoCredentialsData>("/api/video/credentials", refreshKey);
  return <div className="space-y-5">
    <div className="flex items-center gap-2"><h2 className="text-lg font-semibold">视频平台凭据</h2><Badge variant="secondary">仅状态回显</Badge></div>
    <p className="text-sm text-muted-foreground">Cookie 是账号凭据，请勿发到聊天。已保存不代表上游认证有效，HTTP 412 不保证能通过 Cookie 解决。</p>
    {error && <ErrorBanner message={error} />}
    {data && !data.can_write && <ErrorBanner message="当前通道禁止修改凭据。请通过 SSH/Workbench 转发，在 http://127.0.0.1:本地端口 打开面板；不支持公网 HTTP 或仅伪装为 HTTPS 的代理头。" />}
    {loading && !data && <p>加载状态…</p>}
    <div className="grid gap-5 xl:grid-cols-2">{data?.platforms.map(row => <PlatformForm key={row.platform} row={row} canWrite={data.can_write} onSaved={reload} />)}</div>
  </div>;
}

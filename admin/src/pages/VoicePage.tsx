import { useEffect, useRef, useState } from "react";
import { AudioLines, Check, Play, RefreshCw } from "lucide-react";

import { ErrorBanner, InfoRow, SectionCard } from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { VoiceData, VoiceEntry, VoiceProviderId } from "@/lib/analytics";
import { requestBlob, requestJson } from "@/lib/api";
import { formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

const PROVIDER_LABELS: Record<VoiceProviderId, string> = {
  edge: "Edge TTS",
  fish: "Fish Audio",
  stepfun: "StepFun",
};

function voiceKey(provider: string, voice: string): string {
  return `${provider}:${voice}`;
}

export default function VoicePage({ refreshKey }: { refreshKey: number }) {
  const { data, loading, error, reload } = useApi<VoiceData>("/api/voice", refreshKey);
  const [busy, setBusy] = useState("");
  const [previewing, setPreviewing] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const audio = useRef<HTMLAudioElement | null>(null);

  useEffect(
    () => () => {
      audio.current?.pause();
      if (audio.current?.src.startsWith("blob:")) URL.revokeObjectURL(audio.current.src);
    },
    []
  );

  const active = data?.active;
  const activeKey = active ? voiceKey(active.provider, active.voice) : "";
  const choices = data?.catalog ?? [];

  async function switchVoice(entry: { provider: VoiceProviderId; voice: string; name: string }) {
    const key = voiceKey(entry.provider, entry.voice);
    setBusy(`switch:${key}`);
    setActionError("");
    setNotice("");
    try {
      await requestJson<VoiceData>("/api/voice/active", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: entry.provider, voice: entry.voice, name: entry.name }),
      });
      setNotice(`已切换到「${entry.name || entry.voice}」；下一条语音回复立即生效，无需重启。`);
      reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "切换失败");
    } finally {
      setBusy("");
    }
  }

  async function resetVoice() {
    if (!window.confirm("恢复 config.toml 里的默认音色？会删除面板保存的覆盖值。")) return;
    setBusy("reset");
    setActionError("");
    setNotice("");
    try {
      await requestJson<VoiceData>("/api/voice/active", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clear: true }),
      });
      setNotice("已恢复 config.toml 的默认音色。");
      reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "恢复失败");
    } finally {
      setBusy("");
    }
  }

  async function previewVoice(entry: { provider: VoiceProviderId; voice: string; name: string }) {
    const key = voiceKey(entry.provider, entry.voice);
    setPreviewing(key);
    setActionError("");
    try {
      const blob = await requestBlob("/api/voice/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: entry.provider, voice: entry.voice }),
      });
      const url = URL.createObjectURL(blob);
      if (audio.current) {
        audio.current.pause();
        if (audio.current.src.startsWith("blob:")) URL.revokeObjectURL(audio.current.src);
      }
      const player = new Audio(url);
      audio.current = player;
      const clear = () => {
        setPreviewing("");
        URL.revokeObjectURL(url);
      };
      player.onended = clear;
      player.onerror = () => {
        clear();
        setActionError("浏览器无法播放这段试听音频");
      };
      try {
        await player.play();
      } catch {
        clear();
        setActionError("浏览器拦截了播放，请再点一次试听");
      }
    } catch (err) {
      setPreviewing("");
      setActionError(err instanceof Error ? err.message : "试听失败");
    }
  }

  function switchChoice(entry: VoiceEntry) {
    void switchVoice({ provider: entry.provider, voice: entry.id, name: entry.name });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold">音色管理</h2>
        <Badge variant="secondary">在线切换</Badge>
        {loading ? <span className="text-xs text-slate-500">加载中…</span> : null}
      </div>
      <p className="text-sm text-muted-foreground">
        切换只影响之后的语音回复，不需要重启 bot；config.toml 的 [voice] 仍是未切换时的默认值。
        试听使用固定短句，走与 bot 相同的 provider 参数。
      </p>
      {error ? <ErrorBanner message={error} /> : null}
      {actionError ? <ErrorBanner message={actionError} /> : null}
      {notice ? (
        <p role="status" className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
          {notice}
        </p>
      ) : null}
      {data && !data.enabled ? (
        <ErrorBanner message="[voice] enabled = false：模型目前不会使用语音，切换会在开启后才生效。" />
      ) : null}

      {data ? (
        <SectionCard
          icon={AudioLines}
          title="当前音色"
          description={`来源：${data.source === "panel" ? "面板在线切换" : "config.toml"} · 状态文件 ${data.state_file}`}
          badge={data.source === "panel" ? "面板" : "配置"}
          actions={
            <>
              <Button
                variant="outline"
                size="sm"
                disabled={previewing !== ""}
                onClick={() =>
                  void previewVoice({
                    provider: data.active.provider,
                    voice: data.active.voice,
                    name: data.active.name,
                  })
                }
              >
                <Play className="h-3.5 w-3.5" />
                {previewing === activeKey ? "播放中…" : "试听"}
              </Button>
              {data.source === "panel" ? (
                <Button variant="outline" size="sm" disabled={busy !== ""} onClick={() => void resetVoice()}>
                  {busy === "reset" ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : null}
                  恢复配置默认
                </Button>
              ) : null}
            </>
          }
        >
          <div className="grid gap-x-6 md:grid-cols-2">
            <InfoRow label="Provider" value={PROVIDER_LABELS[data.active.provider]} />
            <InfoRow
              label="音色"
              value={data.active.name ? `${data.active.name} · ${data.active.voice}` : data.active.voice}
              mono
            />
            <InfoRow label="模型" value={data.active.model || "—"} mono />
            <InfoRow
              label="密钥"
              value={
                data.active.key_required
                  ? data.active.key_ready
                    ? `${data.active.api_key_env} 已配置`
                    : `${data.active.api_key_env} 未配置`
                  : "无需密钥"
              }
            />
            <InfoRow label="最近切换" value={formatTs(data.updated_at)} />
            <InfoRow
              label="配置默认"
              value={`${PROVIDER_LABELS[data.config_default.provider]} · ${
                data.config_default.name || data.config_default.voice
              }`}
            />
          </div>
        </SectionCard>
      ) : null}

      <SectionCard
        icon={AudioLines}
        title="选择音色"
        description="面板目前只上架这两个 Fish Audio 中文音色；试听后点「切换」立即生效。"
        badge={choices.length}
      >
        <div className="grid gap-4 p-5 md:grid-cols-2">
          {choices.map((entry) => {
            const key = voiceKey(entry.provider, entry.id);
            const current = key === activeKey;
            const listening = previewing === key;
            return (
              <Card
                key={key}
                className={cn(
                  "flex flex-col justify-between transition-shadow",
                  current ? "border-blue-300 bg-blue-50/40" : "hover:shadow-md"
                )}
              >
                <CardHeader className="pb-3">
                  <CardTitle className="flex items-center gap-2 text-base">
                    {entry.name}
                    {current ? (
                      <Badge variant="secondary">
                        <Check className="mr-1 h-3 w-3" />
                        当前
                      </Badge>
                    ) : null}
                  </CardTitle>
                  <CardDescription className="text-xs">{entry.tags || "Fish Audio 中文音色"}</CardDescription>
                  <p className="pt-1 font-mono text-[11px] text-slate-400">{entry.id}</p>
                </CardHeader>
                <CardContent className="flex gap-2 pt-0">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={listening || busy !== ""}
                    onClick={() => void previewVoice({ provider: entry.provider, voice: entry.id, name: entry.name })}
                  >
                    <Play className="h-3.5 w-3.5" />
                    {listening ? "播放中…" : "试听"}
                  </Button>
                  <Button
                    size="sm"
                    variant={current ? "secondary" : "default"}
                    disabled={current || busy !== ""}
                    onClick={() => switchChoice(entry)}
                  >
                    {busy === `switch:${key}` ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : null}
                    {current ? "使用中" : "切换"}
                  </Button>
                </CardContent>
              </Card>
            );
          })}
        </div>
        <p className="px-5 pb-4 text-xs leading-relaxed text-muted-foreground">
          切换结果写入 {data?.state_file ?? "voice-state.json"}；「恢复配置默认」会删除该文件。
          需要其他音色时在 config.toml 的 [voice] 配置，或先加到面板音色目录。
        </p>
      </SectionCard>
    </div>
  );
}

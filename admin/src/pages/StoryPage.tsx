import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { requestJson } from "@/lib/api";

type Settings = {
  stage_timeout: number; prepare_timeout: number; queue_timeout: number; brief_tokens: number; scene_tokens: number;
  output_mode: "legacy" | "text" | "json_object" | "json_schema";
  enabled: boolean; timeout: number; max_tokens: number; max_context_chars: number;
  concurrency: number; menu_hours: number; daily_chat_gain: number; chat_cooldown: number; json_mode: boolean;
  character_preferences: Record<string, { enabled?: boolean; tone?: string; pace?: string; boundaries?: string }>;
};
type Run = { id: string; revision: number; conversation: string; character: string; user: string; stage: string; status: string };
type World = { id: string; conversation: string; character: string; fingerprint: string };
type Operation = { id: string; kind: string; status: string; result: string };
type Delivery = { id: string; run_id: string; status: string; error: string };
type Audit = { actor: string; kind: string; target: string };
type Generation = { conversation: string; purpose: string; status: string; elapsed: number };
type Attempt = { conversation: string; purpose: string; elapsed: number; details: { status: string; attempt: number; finish_reason: string; chars: number; issues: Array<{ path: string; code: string; message: string }> } };
type Draft = { conversation: string; character: string; status: string; error: string; warnings: Array<{ code: string; path: string }> };
type Data = { attempts: Attempt[]; drafts: Draft[]; audit: Audit[]; generations: Generation[]; settings: Settings; runs: Run[]; worlds: World[]; operations: Operation[]; deliveries: Delivery[] };

const labels: Record<keyof Omit<Settings, "enabled" | "json_mode" | "character_preferences" | "output_mode">, string> = {
  stage_timeout: "每阶段总预算（秒）", prepare_timeout: "准备总预算（秒）", queue_timeout: "排队上限（秒）", brief_tokens: "概要输出 token", scene_tokens: "场景输出 token",
  timeout: "单次调用上限（秒）", max_tokens: "最大输出 token", max_context_chars: "上下文字数上限",
  concurrency: "生成并发", menu_hours: "菜单有效期（小时）", daily_chat_gain: "聊天每日正向预算", chat_cooldown: "聊天正向冷却（秒）",
};

export default function StoryPage({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<Data | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [offset, setOffset] = useState(0);
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<unknown>(null);
  const [selected, setSelected] = useState<Run | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [card, setCard] = useState("");
  const [original, setOriginal] = useState("");
  const [conversation, setConversation] = useState("");
  const [preferenceId, setPreferenceId] = useState("");
  const [tone, setTone] = useState("");
  const [pace, setPace] = useState("");
  const [detailQuery, setDetailQuery] = useState<{ id: string; kind: string; offset: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let live = true;
    requestJson<Data>(`/api/story?offset=${offset}`).then(value => {
      if (live) { setData(value); setSettings(value.settings); }
    }).catch(error => { if (live) setNotice(String(error)); });
    return () => { live = false; };
  }, [refreshKey, offset, revision]);

  async function queue(kind: string, payload: object) {
    setBusy(true);
    try {
      const result = await requestJson<{ id: string }>("/api/story/action", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, data: payload }),
      });
      setNotice(`操作 ${result.id.slice(0, 12)} 已入队；刷新查看执行结果。`);
      setRevision(value => value + 1);
      setConfirmation("");
    } catch (error) { setNotice(String(error)); }
    finally { setBusy(false); }
  }
  async function inspect(id: string, kind: string, detailOffset = 0) {
    setDetailQuery({ id, kind, offset: detailOffset });
    try { setDetail(await requestJson(`/api/story/${id}?kind=${kind}&offset=${detailOffset}`)); }
    catch (error) { setNotice(String(error)); }
  }
  const box = "rounded-xl border bg-white p-5 space-y-4";
  return <div className="space-y-6">
    <header><h1 className="text-2xl font-semibold">剧情与关系</h1><p className="mt-2 text-sm text-muted-foreground">共同背景、个人存档。群内故事公开可见，私聊及不同群独立。本期不开放成人内容生成。</p></header>
    {notice && <div role="status" className="rounded-lg border p-3 text-sm">{notice}</div>}
    {settings && <section className={box}>
      <h2 className="font-semibold">运行设置</h2>
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={settings.enabled} onChange={e => setSettings({ ...settings, enabled: e.target.checked })} />启用剧情与新版关系（不自动重开既有存档）</label>
      <div className="grid gap-4 md:grid-cols-3">{Object.entries(labels).map(([key, label]) => <label key={key} className="space-y-2 text-sm"><span>{label}</span><Input type="number" value={settings[key as keyof typeof labels]} onChange={e => setSettings({ ...settings, [key]: Number(e.target.value) })} /></label>)}</div>
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={settings.json_mode} onChange={e => setSettings({ ...settings, json_mode: e.target.checked })} />供应商支持 JSON 模式时启用；默认严格解析兼容输出</label>
      <label className="block space-y-2 text-sm"><span>结构化输出能力（strict 仅供应商已确认支持时选择；普通聊天不受影响）</span><select className="rounded border p-2" value={settings.output_mode} onChange={e => setSettings({ ...settings, output_mode: e.target.value as Settings["output_mode"] })}><option value="legacy">兼容旧 JSON 开关</option><option value="text">提示约束 + 本地验证</option><option value="json_object">JSON mode + 本地验证</option><option value="json_schema">原生 strict schema + 本地验证</option></select></label>
      <Button disabled={busy} onClick={() => queue("settings", settings)}>保存并入队</Button>
    </section>}
    {settings && <section className={box}><h2 className="font-semibold">角色差异与节奏</h2><p className="text-sm text-muted-foreground">仅影响未来生成的世界包；不改写已接受的定义。稳定角色 ID 可从世界包或存档详情复制。</p><div className="grid gap-3 md:grid-cols-3"><Input aria-label="稳定角色 ID" placeholder="完整 character ID（32位）" value={preferenceId} onChange={e => setPreferenceId(e.target.value)} /><Input aria-label="语气偏好" placeholder="语气/价值观偏好（不超过300字）" value={tone} maxLength={300} onChange={e => setTone(e.target.value)} /><Input aria-label="剧情节奏" placeholder="章节节奏/题材偏好（不超过300字）" value={pace} maxLength={300} onChange={e => setPace(e.target.value)} /></div><Button disabled={busy || !/^[a-f0-9]{32}$/.test(preferenceId)} onClick={() => queue("settings", { character_preferences: { ...settings.character_preferences, [preferenceId]: { enabled: true, tone, pace } } })}>保存该角色的生成偏好</Button></section>}
    <section className={box}>
      <div className="flex items-center justify-between"><h2 className="font-semibold">玩家存档</h2><Button variant="outline" onClick={() => setRevision(v => v + 1)}>刷新</Button></div>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">会话 / 玩家</th><th>角色 / 周目</th><th>阶段 / 状态</th><th>操作</th></tr></thead><tbody>{data?.runs.map(run => <tr className="border-t" key={run.id}><td className="p-2">{run.conversation}<br />{run.user}</td><td>{run.character.slice(0, 12)}<br />{run.id.slice(0, 12)} · v{run.revision}</td><td>{run.stage}<br />{run.status}</td><td><Button variant="ghost" onClick={() => { setSelected(run); setConfirmation(""); }}>管理</Button></td></tr>)}</tbody></table></div>
      {!data?.runs.length && <p className="text-sm text-muted-foreground">暂无存档。群内 /story start 开始。</p>}
      <div className="flex gap-2"><Button variant="outline" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</Button><Button variant="outline" disabled={Math.max(data?.runs.length || 0, data?.worlds.length || 0) < 30} onClick={() => setOffset(offset + 30)}>下一页</Button></div>
      {selected && <div className="space-y-3 rounded-lg border p-4 text-sm"><p>目标：{selected.conversation} · {selected.character} · QQ {selected.user} · 周目 {selected.id} · v{selected.revision}</p><p>恢复可能重复展示已发送文字，不会重复计算选择效果。</p><div className="flex flex-wrap gap-2"><Button variant="outline" disabled={busy} onClick={() => inspect(selected.id, "run")}>明确查看剧情详情</Button><Button disabled={busy} onClick={() => queue("retry", { run_id: selected.id, revision: selected.revision, conversation: selected.conversation, character: selected.character, user: selected.user })}>恢复 / 重发当前场景</Button></div><label className="block space-y-2"><span>重开 / 导入旧好感：输入目标 QQ 确认（重开会归档并重置这个目标的关系与剧情）</span><Input value={confirmation} onChange={e => setConfirmation(e.target.value)} placeholder={selected.user} /></label><div className="flex gap-2"><Button variant="destructive" disabled={busy || confirmation !== selected.user} onClick={() => queue("restart", { run_id: selected.id, revision: selected.revision, conversation: selected.conversation, character: selected.character, user: selected.user, confirmed: confirmation })}>重开这个玩家</Button><Button variant="outline" disabled={busy || confirmation !== selected.user} onClick={() => queue("import_legacy", { run_id: selected.id, revision: selected.revision, conversation: selected.conversation, character: selected.character, user: selected.user, confirmed: confirmation })}>导入旧分数（仅空档）</Button></div></div>}
    </section>
    <section className={box}><h2 className="font-semibold">世界包</h2><p className="text-sm text-muted-foreground">定义发布后不可变，现有周目保留原版本，不静默改写路线。</p>{data?.worlds.map(world => <div key={world.id} className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-sm"><span>{world.conversation} · {world.character.slice(0, 12)} · {world.fingerprint.slice(0, 10)}</span><Button variant="outline" onClick={() => inspect(world.id, "world")}>查看定义</Button></div>)}<div className="grid gap-3 md:grid-cols-3"><Input aria-label="目标会话" placeholder="group:123 或 private:123" value={conversation} onChange={e => setConversation(e.target.value)} /><Input aria-label="角色卡 ID" placeholder="角色卡 ID" value={card} onChange={e => setCard(e.target.value)} /><Button disabled={busy || !conversation || !card} onClick={() => queue("regenerate", { conversation, card_id: Number(card) })}>准备缺失世界包</Button></div><div className="grid gap-3 md:grid-cols-3"><Input aria-label="原角色卡 ID" placeholder="明确关联为同一角色：原卡 ID" value={original} onChange={e => setOriginal(e.target.value)} /><span className="text-sm text-muted-foreground">使用上方角色卡 ID 作为新版本；已引用的卡不能重新关联。</span><Button variant="outline" disabled={busy || !card || !original} onClick={() => queue("link", { card_id: Number(card), original_id: Number(original) })}>关联角色版本</Button></div></section>
    <section className={box}><h2 className="font-semibold">生成 / 投递与操作记录</h2>{data?.deliveries.map(row => <p key={row.id} className="text-sm">{row.run_id.slice(0, 12)} · {row.status} · {row.error}</p>)}{data?.operations.map(row => <p key={row.id} className="border-t pt-2 text-sm">{row.id.slice(0, 12)} · {row.kind} · {row.status} · {row.result}</p>)}</section>
    <section className={box}><h2 className="font-semibold">生成诊断与审计（不含正文）</h2>{data?.drafts?.map((row, index) => <p key={`draft-${index}`} className="text-sm">检查点 {row.conversation} · {row.character.slice(0, 12)} · {row.status} · {row.error} · {row.warnings.map(w => w.code).join(", ")}</p>)}{data?.attempts?.map((row, index) => <div key={`attempt-${index}`} className="border-t pt-2 text-sm">{row.conversation} · {row.purpose} · 第{row.details.attempt}次 · {row.details.status} · {row.elapsed.toFixed(1)}s · {row.details.chars}字 · {row.details.finish_reason}<div>{row.details.issues.map(item => `${item.path}: ${item.message}`).join("；")}</div></div>)}{data?.generations?.map((row, index) => <p key={index} className="text-sm">{row.conversation} · {row.purpose} · {row.status} · {row.elapsed.toFixed(1)}s</p>)}{data?.audit?.map((row, index) => <p key={index} className="text-sm">{row.actor} · {row.kind} · {row.target}</p>)}</section>
    {detail !== null && <section className={box}><div className="flex justify-between"><h2 className="font-semibold">明确请求的剧情详情（勿转发私密记录）</h2><Button variant="ghost" onClick={() => setDetail(null)}>关闭</Button></div><div className="flex gap-2"><Button variant="outline" disabled={!detailQuery?.offset} onClick={() => detailQuery && inspect(detailQuery.id, detailQuery.kind, Math.max(0, detailQuery.offset - (detailQuery.kind === "world" ? 8 : 30)))}>详情上一页</Button><Button variant="outline" onClick={() => detailQuery && inspect(detailQuery.id, detailQuery.kind, detailQuery.offset + (detailQuery.kind === "world" ? 8 : 30))}>详情下一页</Button></div><pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-4 text-xs">{JSON.stringify(detail, null, 2)}</pre></section>}
  </div>;
}

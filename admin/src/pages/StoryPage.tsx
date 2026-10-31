import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  FileJson,
  FileSearch,
  Globe2,
  Users,
} from "lucide-react";

import {
  EmptyState,
  FilterBar,
  InfoList,
  InfoRow,
  LoadingHint,
  Notice,
  PageStack,
  SectionCard,
  SectionHeading,
  Toolbar,
} from "@/components/common/Bits";
import { Modal } from "@/components/models/Modal";
import { StoryGraph, StoryNodeDetails } from "@/components/story/StoryGraph";
import type { StoryGraphData } from "@/components/story/StoryGraph";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { requestJson } from "@/lib/api";
import { conversationLabel } from "@/lib/format";

type Run = { id: string; revision: number; conversation: string; character: string; user: string; stage: string; status: string };
type World = { id: string; conversation: string; character: string; fingerprint: string };
type Operation = { id: string; kind: string; status: string; result: string };
type Delivery = { id: string; run_id: string; status: string; error: string };
type Audit = { actor: string; kind: string; target: string };
type Generation = { conversation: string; purpose: string; status: string; elapsed: number };
type Attempt = { conversation: string; purpose: string; elapsed: number; details: { status: string; attempt: number; finish_reason: string; chars: number; issues: Array<{ path: string; code: string; message: string }> } };
type Draft = { conversation: string; character: string; status: string; error: string; warnings: Array<{ code: string; path: string }> };
type Data = { attempts: Attempt[]; drafts: Draft[]; audit: Audit[]; generations: Generation[]; runs: Run[]; worlds: World[]; operations: Operation[]; deliveries: Delivery[] };
type NoticeState = { text: string; tone: "success" | "danger" };

export default function StoryPage({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<Data | null>(null);
  const [offset, setOffset] = useState(0);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [detail, setDetail] = useState<unknown>(null);
  const [selected, setSelected] = useState<Run | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [card, setCard] = useState("");
  const [original, setOriginal] = useState("");
  const [conversation, setConversation] = useState("");
  const [detailQuery, setDetailQuery] = useState<{ id: string; kind: string; offset: number } | null>(null);
  const [graphWorld, setGraphWorld] = useState<World | null>(null);
  const [graph, setGraph] = useState<StoryGraphData | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const graphSeq = useRef(0);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    requestJson<Data>(`/api/story?offset=${offset}`).then(value => {
      if (live) { setData(value); setError(""); }
    }).catch(error => { if (live) setError(String(error)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [refreshKey, offset, revision]);

  async function queue(kind: string, payload: object) {
    setBusy(true);
    try {
      const result = await requestJson<{ id: string }>("/api/story/action", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, data: payload }),
      });
      setNotice({ text: `操作 ${result.id.slice(0, 12)} 已入队；刷新查看执行结果。`, tone: "success" });
      setRevision(value => value + 1);
      setConfirmation("");
    } catch (error) { setNotice({ text: String(error), tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function openGraph(world: World) {
    const seq = ++graphSeq.current;
    setGraphWorld(world);
    setGraph(null);
    setGraphError("");
    setSelectedNodeId(null);
    setGraphLoading(true);
    try {
      const payload = await requestJson<StoryGraphData>(`/api/story/${world.id}?kind=graph`);
      if (seq !== graphSeq.current) return;
      setGraph(payload);
      setGraphError("");
      setSelectedNodeId(null);
    } catch (error) {
      if (seq !== graphSeq.current) return;
      setGraphError(error instanceof Error ? error.message : String(error));
    } finally {
      if (seq === graphSeq.current) setGraphLoading(false);
    }
  }
  function closeGraph() {
    graphSeq.current += 1;
    setGraphWorld(null);
    setGraph(null);
    setGraphError("");
    setGraphLoading(false);
    setSelectedNodeId(null);
  }
  async function inspect(id: string, kind: string, detailOffset = 0) {
    setDetailQuery({ id, kind, offset: detailOffset });
    try { setDetail(await requestJson(`/api/story/${id}?kind=${kind}&offset=${detailOffset}`)); }
    catch (error) { setNotice({ text: String(error), tone: "danger" }); }
  }

  const runs = data?.runs ?? [];
  const worlds = data?.worlds ?? [];
  const selectedNode = graph?.nodes.find(node => node.id === selectedNodeId) ?? null;
  const hasDiagnostics = Boolean(
    data?.deliveries.length || data?.operations.length || data?.drafts.length ||
    data?.attempts.length || data?.generations.length || data?.audit.length
  );

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}


      <SectionCard icon={Users} title="玩家存档" badge={runs.length}>
        <FilterBar actions={<Button variant="outline" size="sm" onClick={() => setRevision(v => v + 1)}>刷新</Button>}>
          <span className="text-xs text-slate-500">每页 30 条</span>
        </FilterBar>
        {runs.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>会话 / 玩家</TableHead>
                <TableHead>角色 / 周目</TableHead>
                <TableHead>阶段 / 状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map(run => (
                <TableRow key={run.id}>
                  <TableCell>
                    <div className="text-slate-800">{conversationLabel(run.conversation)}</div>
                    <div className="text-xs text-slate-500">{run.user}</div>
                  </TableCell>
                  <TableCell>
                    <div className="font-mono text-xs text-slate-800" title={run.character}>
                      {run.character.slice(0, 12)}…
                    </div>
                    <div className="font-mono text-xs text-slate-500" title={run.id}>
                      {run.id.slice(0, 12)}… · v{run.revision}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="text-slate-800">{run.stage}</div>
                    <div className="text-xs text-slate-500">{run.status}</div>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`管理 ${run.conversation} 的存档`}
                      onClick={() => { setSelected(run); setConfirmation(""); }}
                    >
                      管理
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <EmptyState icon={BookOpen} text="暂无存档" hint="群内发送 /story start 开始。" />
        )}
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-100 px-4 py-3">
          <Button variant="outline" size="sm" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 30))}>
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={Math.max(runs.length, worlds.length) < 30}
            onClick={() => setOffset(offset + 30)}
          >
            下一页
          </Button>
        </div>

        {selected ? (
          <div className="border-t border-slate-100 p-4">
            <div className="space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-4">
              <InfoList className="rounded-lg bg-white px-1">
                <InfoRow label="会话" value={conversationLabel(selected.conversation)} />
                <InfoRow label="角色" value={selected.character} mono />
                <InfoRow label="玩家 QQ" value={selected.user} mono />
                <InfoRow label="周目" value={`${selected.id} · v${selected.revision}`} mono />
              </InfoList>
              <Notice tone="warning">恢复可能重复展示已发送文字，不会重复计算选择效果。</Notice>
              <Toolbar>
                <Button variant="outline" disabled={busy} onClick={() => inspect(selected.id, "run")}>
                  明确查看剧情详情
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => queue("retry", { run_id: selected.id, revision: selected.revision, conversation: selected.conversation, character: selected.character, user: selected.user })}
                >
                  恢复 / 重发当前场景
                </Button>
              </Toolbar>
              <Field
                label="输入目标 QQ 确认"
                htmlFor="confirm-user"
                hint="重开会归档并重置这个目标的关系与剧情。"
              >
                <Input
                  id="confirm-user"
                  value={confirmation}
                  onChange={e => setConfirmation(e.target.value)}
                  placeholder={selected.user}
                />
              </Field>
              <Toolbar>
                <Button
                  variant="destructive"
                  disabled={busy || confirmation !== selected.user}
                  onClick={() => queue("restart", { run_id: selected.id, revision: selected.revision, conversation: selected.conversation, character: selected.character, user: selected.user, confirmed: confirmation })}
                >
                  重开这个玩家
                </Button>
                <Button
                  variant="outline"
                  disabled={busy || confirmation !== selected.user}
                  onClick={() => queue("import_legacy", { run_id: selected.id, revision: selected.revision, conversation: selected.conversation, character: selected.character, user: selected.user, confirmed: confirmation })}
                >
                  导入旧分数（仅空档）
                </Button>
              </Toolbar>
            </div>
          </div>
        ) : null}
      </SectionCard>

      <SectionCard
        icon={Globe2}
        title="世界包"
        badge={worlds.length}
        description="定义发布后不可变，现有周目保留原版本。"
      >
        {worlds.length ? (
          <div className="divide-y divide-slate-100">
            {worlds.map(world => (
              <div key={world.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-sm">
                <span className="min-w-0 break-all text-slate-700">
                  {conversationLabel(world.conversation)} ·{" "}
                  <span title={world.character}>{world.character.slice(0, 12)}…</span> ·{" "}
                  <span className="font-mono text-xs text-slate-500" title={world.fingerprint}>
                    {world.fingerprint.slice(0, 10)}…
                  </span>
                </span>
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" onClick={() => void openGraph(world)}>
                    剧情树
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => inspect(world.id, "world")}>
                    查看定义
                  </Button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon={Globe2} text="暂无世界包" hint="可在下方准备缺失的世界包。" />
        )}

        <div className="space-y-4 border-t border-slate-100 p-5">
          <div className="grid items-end gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Field label="目标会话" htmlFor="world-conversation">
              <Input
                id="world-conversation"
                placeholder="例：group:123456 或 private:123456"
                value={conversation}
                onChange={e => setConversation(e.target.value)}
              />
            </Field>
            <Field label="角色卡 ID" htmlFor="world-card">
              <Input
                id="world-card"
                placeholder="角色卡 ID"
                value={card}
                onChange={e => setCard(e.target.value)}
              />
            </Field>
            <Button
              className="sm:col-span-2 xl:col-span-2"
              disabled={busy || !conversation || !card}
              onClick={() => queue("regenerate", { conversation, card_id: Number(card) })}
            >
              准备缺失世界包
            </Button>
          </div>

          <div className="grid items-end gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Field label="原角色卡 ID" htmlFor="world-original">
              <Input
                id="world-original"
                placeholder="原卡 ID"
                value={original}
                onChange={e => setOriginal(e.target.value)}
              />
            </Field>
            <p className="text-xs leading-relaxed text-slate-500 sm:col-span-2 xl:col-span-1">
              使用上方角色卡 ID 作为新版本；已引用的卡不能重新关联。
            </p>
            <Button
              variant="outline"
              disabled={busy || !card || !original}
              onClick={() => queue("link", { card_id: Number(card), original_id: Number(original) })}
            >
              关联角色版本
            </Button>
          </div>
        </div>
      </SectionCard>

      <Card className="overflow-hidden">
        <details>
          <summary className="cursor-pointer list-none px-5 py-4 text-sm font-semibold text-slate-900 [&::-webkit-details-marker]:hidden">
            诊断与审计（不含正文）
          </summary>
          <div className="border-t border-slate-100 p-5">
            {hasDiagnostics ? (
              <div className="space-y-4">
                <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <SectionHeading title="投递与操作" />
                  <div className="mt-2 divide-y divide-slate-200 text-sm text-slate-700">
                    {data?.deliveries.map(row => (
                      <p key={row.id} className="break-all py-1.5">
                        {row.run_id.slice(0, 12)} · {row.status} · {row.error}
                      </p>
                    ))}
                    {data?.operations.map(row => (
                      <p key={row.id} className="break-all py-1.5">
                        {row.id.slice(0, 12)} · {row.kind} · {row.status} · {row.result}
                      </p>
                    ))}
                  </div>
                </div>

                <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <SectionHeading title="生成诊断" />
                  <div className="mt-2 divide-y divide-slate-200 text-sm text-slate-700">
                    {data?.drafts.map((row, index) => (
                      <p key={`draft-${index}`} className="break-all py-1.5">
                        草稿 {conversationLabel(row.conversation)} · {row.character.slice(0, 12)}… · {row.status} · {row.error} ·{" "}
                        {row.warnings.map(w => w.code).join(", ")}
                      </p>
                    ))}
                    {data?.attempts.map((row, index) => (
                      <div key={`attempt-${index}`} className="break-all py-1.5">
                        <p>
                          {conversationLabel(row.conversation)} · {row.purpose} · 第 {row.details.attempt} 次 · {row.details.status} ·{" "}
                          {row.elapsed.toFixed(1)}s · {row.details.chars} 字 · {row.details.finish_reason}
                        </p>
                        {row.details.issues.length ? (
                          <p className="text-xs text-slate-500">
                            {row.details.issues.map(item => `${item.path}: ${item.message}`).join("；")}
                          </p>
                        ) : null}
                      </div>
                    ))}
                    {data?.generations.map((row, index) => (
                      <p key={`generation-${index}`} className="break-all py-1.5">
                        {conversationLabel(row.conversation)} · {row.purpose} · {row.status} · {row.elapsed.toFixed(1)}s
                      </p>
                    ))}
                  </div>
                </div>

                <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <SectionHeading title="审计" />
                  <div className="mt-2 divide-y divide-slate-200 text-sm text-slate-700">
                    {data?.audit.map((row, index) => (
                      <p key={`audit-${index}`} className="break-all py-1.5">
                        {row.actor} · {row.kind} · {row.target}
                      </p>
                    ))}
                  </div>
                </div>
              </div>
            ) : (
              <EmptyState icon={FileSearch} text="暂无诊断记录" />
            )}
          </div>
        </details>
      </Card>

      {detail !== null ? (
        <SectionCard
          icon={FileJson}
          title="剧情详情"
          description="按需加载；勿转发私密记录。"
          actions={
            <Button variant="ghost" size="sm" aria-label="关闭剧情详情" onClick={() => setDetail(null)}>
              关闭
            </Button>
          }
        >
          <div className="space-y-3 p-5">
            <Toolbar>
              <Button
                variant="outline"
                size="sm"
                disabled={!detailQuery?.offset}
                onClick={() => detailQuery && inspect(detailQuery.id, detailQuery.kind, Math.max(0, detailQuery.offset - (detailQuery.kind === "world" ? 8 : 30)))}
              >
                上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => detailQuery && inspect(detailQuery.id, detailQuery.kind, detailQuery.offset + (detailQuery.kind === "world" ? 8 : 30))}
              >
                下一页
              </Button>
            </Toolbar>
            <pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-all rounded-xl border border-slate-200 bg-slate-50 p-4 text-xs">
              {JSON.stringify(detail, null, 2)}
            </pre>
          </div>
        </SectionCard>
      ) : null}
      <Modal
        open={graphWorld !== null}
        title={graph?.definition.title || "剧情树"}
        description={
          graphWorld
            ? graph
              ? `${conversationLabel(graphWorld.conversation)} · ${graph.total} 个节点`
              : graphLoading
                ? "正在加载剧情树…"
                : undefined
            : undefined
        }
        onClose={closeGraph}
        wide
        panelClassName="max-w-6xl"
        footer={
          <Button variant="outline" size="sm" onClick={closeGraph}>
            关闭
          </Button>
        }
      >
        {graphLoading ? (
          <div className="flex h-64 items-center justify-center">
            <LoadingHint loading label="正在加载剧情树…" />
          </div>
        ) : graphError ? (
          <div className="space-y-3">
            <Notice tone="danger">{graphError}</Notice>
            <Button
              variant="outline"
              size="sm"
              onClick={() => graphWorld && void openGraph(graphWorld)}
            >
              重试
            </Button>
          </div>
        ) : graph ? (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <StoryGraph
              data={graph}
              selectedId={selectedNodeId}
              onSelect={node => setSelectedNodeId(node ? node.id : null)}
            />
            <StoryNodeDetails
              node={selectedNode}
              nodes={graph.nodes}
              start={graph.definition.start}
              onSelect={setSelectedNodeId}
            />
          </div>
        ) : null}
      </Modal>
    </PageStack>
  );
}

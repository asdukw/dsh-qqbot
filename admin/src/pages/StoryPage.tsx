import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  BookOpen,
  Check,
  Copy,
  FileJson,
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
  PillTabs,
  SectionCard,
  Toolbar,
} from "@/components/common/Bits";
import { StoryGraph, StoryNodeDetails } from "@/components/story/StoryGraph";
import type { StoryGraphData } from "@/components/story/StoryGraph";
import { Button } from "@/components/ui/button";
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
type World = {
  id: string;
  conversation: string;
  conversations?: string[];
  conversation_count?: number;
  character: string;
  character_name?: string;
  title?: string;
  fingerprint: string;
};
type Data = { runs: Run[]; worlds: World[] };
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
  const [tab, setTab] = useState<"graph" | "definition">("graph");
  const [copied, setCopied] = useState(false);
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
    setTab("graph");
    setCopied(false);
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
  function copyDefinition() {
    if (!graph) return;
    const text = JSON.stringify({ definition: graph.definition, nodes: graph.nodes }, null, 2);
    navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  }

  async function inspect(id: string, kind: string, detailOffset = 0) {
    setDetailQuery({ id, kind, offset: detailOffset });
    try { setDetail(await requestJson(`/api/story/${id}?kind=${kind}&offset=${detailOffset}`)); }
    catch (error) { setNotice({ text: String(error), tone: "danger" }); }
  }

  const runs = data?.runs ?? [];
  const worlds = data?.worlds ?? [];
  const selectedNode = graph?.nodes.find(node => node.id === selectedNodeId) ?? null;

  if (graphWorld) {
    const conversations = graphWorld.conversations ?? [graphWorld.conversation];
    return (
      <PageStack error={error} loading={loading} hasData={!!data}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button variant="ghost" size="sm" onClick={closeGraph}>
            <ArrowLeft className="h-4 w-4" />
            返回世界包
          </Button>
          <span className="text-xs text-slate-500">{conversations.length} 个会话</span>
        </div>
        <SectionCard
          icon={Globe2}
          title={graph?.definition.title || graphWorld.title || "世界包详情"}
          description={conversations.map(id => conversationLabel(id)).join(" · ")}
          actions={
            <PillTabs
              value={tab}
              options={[
                { value: "graph", label: "剧情树" },
                { value: "definition", label: "定义" },
              ]}
              onChange={setTab}
              label="世界包详情分区"
            />
          }
        >
          <div className="p-5">
            {graphLoading ? (
              <div className="flex h-64 items-center justify-center">
                <LoadingHint loading label="正在加载世界包…" />
              </div>
            ) : graphError ? (
              <div className="space-y-3">
                <Notice tone="danger">{graphError}</Notice>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void openGraph(graphWorld)}
                >
                  重试
                </Button>
              </div>
            ) : graph ? (
              tab === "graph" ? (
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
              ) : (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs text-slate-500">完整定义（只读）</p>
                    <Button variant="outline" size="sm" onClick={copyDefinition}>
                      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                      {copied ? "已复制" : "复制 JSON"}
                    </Button>
                  </div>
                  <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-xl border border-slate-200 bg-slate-50 p-4 text-xs">
                    {JSON.stringify({ definition: graph.definition, nodes: graph.nodes }, null, 2)}
                  </pre>
                </div>
              )
            ) : null}
          </div>
        </SectionCard>
      </PageStack>
    );
  }

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
                <span className="min-w-0">
                  <span
                    className="block truncate text-sm font-medium text-slate-800"
                    title={world.character}
                  >
                    {world.character_name || world.title || `角色 #${world.character.slice(0, 12)}`}
                  </span>
                  <span className="block truncate text-xs text-slate-500">
                    {(world.conversations ?? [world.conversation]).map(id => conversationLabel(id)).join(" · ")}
                    {world.character_name && world.title ? ` · ${world.title}` : ""}
                  </span>
                </span>
                <Button variant="outline" size="sm" onClick={() => void openGraph(world)}>
                  详情
                </Button>
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
    </PageStack>
  );
}

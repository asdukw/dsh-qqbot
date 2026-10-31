import { useEffect, useMemo, useRef, useState } from "react";
import { Maximize2, Minus, Plus } from "lucide-react";

import { Notice } from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type StoryEffect = {
  affinity: number;
  trust: number;
  flags: string[];
  milestone: string;
  intent: string;
  reason: string;
};

export type StoryChoice = {
  id: string;
  text: string;
  target: string;
  condition: { flags: string[]; not_flags: string[]; affinity: number; trust: number };
  effect: StoryEffect;
  once: boolean;
  delayed: Array<{ id: string; after_turns: number; effect: StoryEffect }>;
};

export type StoryNode = {
  id: string;
  chapter: string;
  beat: string;
  choices: StoryChoice[];
  ending: string;
};

export type StoryGraphData = {
  id: string;
  conversation: string;
  character: string;
  fingerprint: string;
  created: number;
  definition: {
    title: string;
    background: string;
    synopsis: string;
    motivation: string;
    character: string;
    public_character: string;
    behavior: Record<string, string>;
    start: string;
    version: number;
  };
  nodes: StoryNode[];
  total: number;
};

const NODE_W = 196;
const NODE_H = 98;
const GAP_X = 88;
const GAP_Y = 26;
const PAD = 24;

const MILESTONE_LABELS: Record<string, string> = {
  shared_experience: "共同经历",
  promise: "约定",
  conflict: "冲突",
  repair: "修复",
};

const INTENT_LABELS: Record<string, string> = {
  friendship: "友情",
  offer_romance: "试探恋情",
  accept_romance: "接受恋情",
  decline: "拒绝",
  boundary: "边界",
};

type Position = { x: number; y: number; level: number };

function layoutGraph(start: string, nodes: StoryNode[]) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const levels = new Map<string, number>();
  if (byId.has(start)) levels.set(start, 0);
  const queue = byId.has(start) ? [start] : [];
  while (queue.length) {
    const id = queue.shift()!;
    const node = byId.get(id);
    if (!node) continue;
    const level = levels.get(id) ?? 0;
    for (const choice of node.choices) {
      if (!byId.has(choice.target)) continue;
      const next = level + 1;
      const current = levels.get(choice.target);
      if (current === undefined || next < current) {
        levels.set(choice.target, next);
        queue.push(choice.target);
      }
    }
  }
  const reachableMax = Math.max(0, ...levels.values());
  for (const node of nodes) {
    if (!levels.has(node.id)) levels.set(node.id, reachableMax + 1);
  }

  const columns = new Map<number, StoryNode[]>();
  for (const node of nodes) {
    const level = levels.get(node.id) ?? 0;
    const column = columns.get(level);
    if (column) column.push(node);
    else columns.set(level, [node]);
  }
  const order = [...columns.keys()].sort((a, b) => a - b);
  const maxCount = Math.max(...order.map((level) => columns.get(level)!.length));
  const height = PAD * 2 + maxCount * NODE_H + Math.max(0, maxCount - 1) * GAP_Y;
  const width =
    PAD * 2 + order.length * NODE_W + Math.max(0, order.length - 1) * GAP_X;
  const positions = new Map<string, Position>();
  order.forEach((level, columnIndex) => {
    const column = columns.get(level)!;
    const columnHeight = column.length * NODE_H + Math.max(0, column.length - 1) * GAP_Y;
    const top = PAD + (height - PAD * 2 - columnHeight) / 2;
    column.forEach((node, rowIndex) => {
      positions.set(node.id, {
        x: PAD + columnIndex * (NODE_W + GAP_X),
        y: top + rowIndex * (NODE_H + GAP_Y),
        level,
      });
    });
  });
  return { positions, width, height, reachableMax };
}

function edgePath(from: Position, to: Position): string {
  const x1 = from.x + NODE_W;
  const y1 = from.y + NODE_H / 2;
  const x2 = to.x;
  const y2 = to.y + NODE_H / 2;
  if (x2 > x1) {
    const bend = Math.max(36, (x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  }
  const bend = 48;
  const loopY = Math.min(y1, y2) - 34;
  return `M ${x1} ${y1} C ${x1 + bend} ${loopY}, ${x2 - bend} ${loopY}, ${x2} ${y2}`;
}

function conditionText(condition: StoryChoice["condition"]): string {
  const parts: string[] = [];
  if (condition.flags.length) parts.push(`需要 ${condition.flags.join("、")}`);
  if (condition.not_flags.length) parts.push(`不能有 ${condition.not_flags.join("、")}`);
  if (condition.affinity) parts.push(`好感 ≥ ${condition.affinity}`);
  if (condition.trust) parts.push(`信任 ≥ ${condition.trust}`);
  return parts.join(" · ");
}

function effectText(effect: StoryEffect): string {
  const parts: string[] = [];
  if (effect.affinity) parts.push(`好感 ${effect.affinity > 0 ? "+" : ""}${effect.affinity}`);
  if (effect.trust) parts.push(`信任 ${effect.trust > 0 ? "+" : ""}${effect.trust}`);
  if (effect.flags.length) parts.push(`标记 ${effect.flags.join("、")}`);
  if (effect.milestone) parts.push(MILESTONE_LABELS[effect.milestone] || effect.milestone);
  if (effect.intent) parts.push(INTENT_LABELS[effect.intent] || effect.intent);
  if (effect.reason) parts.push(`“${effect.reason}”`);
  return parts.join(" · ");
}

export function StoryGraph({
  data,
  selectedId,
  onSelect,
}: {
  data: StoryGraphData;
  selectedId: string | null;
  onSelect: (node: StoryNode) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const { positions, width, height, reachableMax } = useMemo(
    () => layoutGraph(data.definition.start, data.nodes),
    [data]
  );
  const byId = useMemo(() => new Map(data.nodes.map((node) => [node.id, node])), [data.nodes]);
  const edges = useMemo(
    () =>
      data.nodes.flatMap((node) =>
        node.choices
          .filter((choice) => byId.has(choice.target))
          .map((choice) => ({
            key: `${node.id}:${choice.id}:${choice.target}`,
            from: node.id,
            to: choice.target,
            text: choice.text,
          }))
      ),
    [data.nodes, byId]
  );

  const related = useMemo(() => {
    const set = new Set<string>();
    if (!selectedId) return set;
    set.add(selectedId);
    for (const edge of edges) {
      if (edge.from === selectedId) set.add(edge.to);
      if (edge.to === selectedId) set.add(edge.from);
    }
    return set;
  }, [edges, selectedId]);

  function fit() {
    const container = scrollRef.current;
    if (!container || container.clientWidth < 80) return;
    const next = Math.min(1, (container.clientWidth - 24) / width);
    setZoom(Math.max(0.4, Number(next.toFixed(2))));
  }

  useEffect(() => {
    fit();
    // Fit once per graph; the container width is stable enough for this panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width]);

  useEffect(() => {
    const container = scrollRef.current;
    const position = selectedId ? positions.get(selectedId) : null;
    if (!container || !position) return;
    const left = position.x * zoom;
    const top = position.y * zoom;
    const right = left + NODE_W * zoom;
    const bottom = top + NODE_H * zoom;
    const outside =
      left < container.scrollLeft ||
      right > container.scrollLeft + container.clientWidth ||
      top < container.scrollTop ||
      bottom > container.scrollTop + container.clientHeight;
    if (!outside) return;
    container.scrollTo({
      left: Math.max(0, left - 24),
      top: Math.max(0, top - 24),
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  }, [selectedId, positions, zoom]);

  return (
    <div className="min-w-0">
      <div
        ref={scrollRef}
        className="relative h-[52vh] min-h-[320px] overflow-auto rounded-xl border border-slate-200 bg-slate-50"
      >
        <div className="sticky left-0 top-0 z-20 flex justify-end p-1.5">
          <div className="flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white/95 p-0.5 shadow-sm">
            <Button
              variant="ghost"
              size="icon"
              aria-label="缩小"
              onClick={() => setZoom((value) => Math.max(0.4, Number((value - 0.1).toFixed(2))))}
            >
              <Minus className="h-3.5 w-3.5" />
            </Button>
            <span className="w-10 text-center text-[11px] tabular-nums text-slate-500">
              {Math.round(zoom * 100)}%
            </span>
            <Button
              variant="ghost"
              size="icon"
              aria-label="放大"
              onClick={() => setZoom((value) => Math.min(1.6, Number((value + 0.1).toFixed(2))))}
            >
              <Plus className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" aria-label="适应宽度" onClick={fit}>
              <Maximize2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        <div style={{ width: width * zoom, height: height * zoom }}>
          <div
            className="relative"
            style={{ width, height, transform: `scale(${zoom})`, transformOrigin: "top left" }}
          >
            <svg
              width={width}
              height={height}
              className="absolute inset-0"
              aria-hidden="true"
              focusable="false"
            >
              <defs>
                <marker
                  id="story-arrow"
                  viewBox="0 0 10 10"
                  refX="8"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
                </marker>
              </defs>
              {edges.map((edge) => {
                const from = positions.get(edge.from);
                const to = positions.get(edge.to);
                if (!from || !to) return null;
                const dim = selectedId !== null && !(edge.from === selectedId || edge.to === selectedId);
                return (
                  <path
                    key={edge.key}
                    d={edgePath(from, to)}
                    fill="none"
                    stroke={edge.from === selectedId ? "#2563eb" : "#94a3b8"}
                    strokeWidth={edge.from === selectedId ? 2 : 1.4}
                    strokeDasharray={to.level <= from.level ? "5 4" : undefined}
                    markerEnd="url(#story-arrow)"
                    opacity={dim ? 0.25 : 0.9}
                  />
                );
              })}
              {selectedId
                ? edges
                    .filter((edge) => edge.from === selectedId)
                    .map((edge) => {
                      const from = positions.get(edge.from);
                      const to = positions.get(edge.to);
                      if (!from || !to) return null;
                      const x = (from.x + NODE_W + to.x) / 2;
                      const y =
                        (from.y + NODE_H / 2 + to.y + NODE_H / 2) / 2 -
                        (to.x > from.x ? 8 : 30);
                      const label = edge.text.length > 12 ? `${edge.text.slice(0, 12)}…` : edge.text;
                      return (
                        <text
                          key={`label-${edge.key}`}
                          x={x}
                          y={y}
                          textAnchor="middle"
                          fontSize={10}
                          fill="#475569"
                          stroke="#ffffff"
                          strokeWidth={3}
                          paintOrder="stroke"
                        >
                          {label}
                        </text>
                      );
                    })
                : null}
            </svg>

            {data.nodes.map((node) => {
              const position = positions.get(node.id);
              if (!position) return null;
              const isStart = node.id === data.definition.start;
              const unreachable = position.level > reachableMax;
              const selected = node.id === selectedId;
              const dim = selectedId !== null && !related.has(node.id);
              return (
                <button
                  key={node.id}
                  type="button"
                  onClick={() => onSelect(node)}
                  aria-pressed={selected}
                  title={node.beat}
                  style={{
                    left: position.x,
                    top: position.y,
                    width: NODE_W,
                    height: NODE_H,
                  }}
                  className={cn(
                    "absolute z-10 flex flex-col rounded-xl border bg-white p-2.5 text-left shadow-sm transition-shadow hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                    selected
                      ? "border-blue-500 ring-2 ring-blue-200"
                      : isStart
                        ? "border-emerald-300"
                        : node.ending
                          ? "border-amber-300"
                          : "border-slate-200",
                    unreachable && "border-dashed opacity-60",
                    dim && !selected && "opacity-30"
                  )}
                >
                  <span className="flex items-center gap-1 overflow-hidden">
                    <span className="truncate font-mono text-[10px] text-slate-400">{node.id}</span>
                    {isStart ? <Badge variant="success">起点</Badge> : null}
                    {node.ending ? <Badge variant="warning">结局</Badge> : null}
                    {unreachable ? <Badge variant="muted">不可达</Badge> : null}
                  </span>
                  <span className="mt-1 line-clamp-1 text-[11px] font-semibold text-slate-800">
                    {node.chapter}
                  </span>
                  <span className="mt-0.5 line-clamp-2 text-[11px] leading-4 text-slate-500">
                    {node.beat}
                  </span>
                  <span className="mt-auto truncate text-[10px] text-slate-400">
                    {node.ending ? `结局：${node.ending}` : `${node.choices.length} 个选项`}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-emerald-500" /> 起点
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-amber-400" /> 结局
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-slate-300" /> 分支
        </span>
        <span>点击节点查看详情；选中后高亮相邻节点，蓝色曲线标注选项。</span>
      </div>

      <details className="mt-2 rounded-xl border border-slate-200">
        <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-slate-600">
          以列表查看全部节点
        </summary>
        <div className="grid gap-1 border-t border-slate-100 p-2 sm:grid-cols-2">
          {data.nodes.map((node) => (
            <button
              key={node.id}
              type="button"
              onClick={() => onSelect(node)}
              className={cn(
                "rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors",
                node.id === selectedId
                  ? "bg-blue-50 text-blue-700"
                  : "text-slate-600 hover:bg-slate-50"
              )}
            >
              <span className="font-mono text-[10px] text-slate-400">{node.id}</span>
              <span className="ml-2 font-medium">{node.chapter}</span>
              <span className="ml-1 text-slate-400">
                {node.ending ? "结局" : `${node.choices.length} 选项`}
              </span>
            </button>
          ))}
        </div>
      </details>
    </div>
  );
}

export function StoryNodeDetails({
  node,
  nodes,
  start,
  onSelect,
}: {
  node: StoryNode | null;
  nodes: StoryNode[];
  start: string;
  onSelect: (id: string) => void;
}) {
  const byId = useMemo(() => new Map(nodes.map((item) => [item.id, item])), [nodes]);
  if (!node) {
    return (
      <div className="flex h-full min-h-[18rem] items-center justify-center rounded-xl border border-dashed border-slate-200 px-4 text-center text-sm text-slate-500">
        点击左侧节点查看章节、选项、条件与效果。
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-[18rem] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white">
      <div className="border-b border-slate-100 p-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[11px] text-slate-400">#{node.id}</span>
          {node.id === start ? <Badge variant="success">起点</Badge> : null}
          {node.ending ? <Badge variant="warning">结局</Badge> : null}
        </div>
        <h3 className="mt-1 text-sm font-semibold text-slate-900">{node.chapter}</h3>
        <p className="mt-1 text-xs leading-relaxed text-slate-600">{node.beat}</p>
      </div>
      <div className="flex-1 space-y-2.5 overflow-y-auto p-4">
        {node.ending ? <Notice tone="success">结局：{node.ending}</Notice> : null}
        {node.choices.length ? (
          node.choices.map((choice) => {
            const target = byId.get(choice.target);
            const condition = conditionText(choice.condition);
            const effect = effectText(choice.effect);
            return (
              <div key={choice.id} className="rounded-lg border border-slate-200 p-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs font-semibold text-slate-800">{choice.text}</span>
                  {choice.once ? <Badge variant="outline">仅一次</Badge> : null}
                </div>
                <button
                  type="button"
                  onClick={() => onSelect(choice.target)}
                  className="mt-1 inline-flex items-center gap-1 text-[11px] text-blue-600 hover:text-blue-700 hover:underline"
                >
                  → {target ? target.chapter : choice.target}
                  <span className="font-mono text-slate-400">#{choice.target}</span>
                </button>
                <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
                  条件：{condition || "无条件"}
                </p>
                <p className="text-[11px] leading-relaxed text-slate-500">
                  效果：{effect || "无"}
                </p>
                {choice.delayed.length ? (
                  <p className="text-[11px] leading-relaxed text-slate-500">
                    延迟：
                    {choice.delayed
                      .map(
                        (item) =>
                          `${item.after_turns} 回合后 ${effectText(item.effect) || "无"}`
                      )
                      .join("；")}
                  </p>
                ) : null}
              </div>
            );
          })
        ) : (
          <p className="text-xs text-slate-500">这是结局节点，没有后续选项。</p>
        )}
      </div>
    </div>
  );
}

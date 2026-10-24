import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { BookOpen, Check, Copy } from "lucide-react";

import { EmptyState, ErrorBanner, PillTabs } from "@/components/common/Bits";
import { Modal } from "@/components/models/Modal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { CardDataV3, CardDetail, LoreEntryV3 } from "@/lib/analytics";
import { formatNumber, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type Tab = "overview" | "greetings" | "prompts" | "lorebook" | "meta" | "json";

const MACRO = /(\{\{[^{}]+\}\}|<START>)/g;

// Highlight {{char}} / {{user}} style macros so placeholders stand out.
function MacroText({ text }: { text: string }) {
  const parts = text.split(MACRO);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <span key={index} className="rounded bg-blue-50 px-1 font-mono text-[0.85em] text-blue-700">
            {part}
          </span>
        ) : (
          part
        )
      )}
    </>
  );
}

function TextBlock({
  label,
  text,
  hint,
  badge,
}: {
  label: string;
  text?: string;
  hint?: string;
  badge?: ReactNode;
}) {
  const value = (text ?? "").trim();
  return (
    <section className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-semibold text-slate-700">{label}</h3>
        {badge}
        {hint ? <span className="font-mono text-[10px] text-slate-400">{hint}</span> : null}
        <span className="ml-auto text-[10px] text-slate-400">
          {value ? `${formatNumber(value.length)} 字` : ""}
        </span>
      </div>
      {value ? (
        <div className="whitespace-pre-wrap break-words rounded-lg border border-blue-50 bg-slate-50/60 px-3 py-2.5 text-sm leading-relaxed text-slate-700">
          <MacroText text={value} />
        </div>
      ) : (
        <p className="rounded-lg border border-dashed border-slate-200 px-3 py-2 text-xs text-slate-400">
          未填写
        </p>
      )}
    </section>
  );
}

function MetaGrid({ rows }: { rows: Array<[string, ReactNode, boolean?]> }) {
  return (
    <dl className="grid gap-x-6 gap-y-2 rounded-lg border border-blue-50 px-4 py-3 sm:grid-cols-2">
      {rows.map(([label, value, mono]) => (
        <div key={label} className="flex min-w-0 items-baseline justify-between gap-3">
          <dt className="shrink-0 text-xs text-slate-500">{label}</dt>
          <dd
            className={cn(
              "min-w-0 truncate text-right text-sm text-slate-800",
              mono && "font-mono text-xs"
            )}
            title={typeof value === "string" ? value : undefined}
          >
            {value === "" || value == null ? "-" : value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Chips({
  items,
  empty,
  variant = "secondary",
}: {
  items: string[];
  empty: string;
  variant?: "secondary" | "outline" | "muted";
}) {
  if (!items.length) return <span className="text-xs text-muted-foreground">{empty}</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item, index) => (
        <Badge key={`${item}-${index}`} variant={variant}>
          {item}
        </Badge>
      ))}
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-[50vh] overflow-auto rounded-lg border border-blue-50 bg-slate-900 px-3 py-2.5 font-mono text-[11px] leading-relaxed text-slate-100">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function isEmptyObject(value: unknown) {
  return !value || (typeof value === "object" && Object.keys(value as object).length === 0);
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

// SillyTavern stores example dialogues as <START>-separated blocks.
function exampleBlocks(text: string): string[] {
  return text
    .split(/<START>/i)
    .map((block) => block.trim())
    .filter(Boolean);
}

function OverviewTab({ detail, data }: { detail: CardDetail; data: CardDataV3 }) {
  const multilingual = Object.entries(data.creator_notes_multilingual ?? {});
  return (
    <div className="space-y-4">
      <MetaGrid
        rows={[
          ["名称 name", data.name || detail.name],
          ["昵称 nickname", data.nickname || detail.nickname],
          ["作者 creator", data.creator || detail.creator],
          ["版本 character_version", data.character_version || detail.character_version],
          ["创建 creation_date", formatTs(data.creation_date)],
          ["修改 modification_date", formatTs(data.modification_date)],
        ]}
      />
      <div className="space-y-1.5">
        <h3 className="text-xs font-semibold text-slate-700">标签 tags</h3>
        <Chips items={strings(data.tags ?? detail.tags)} empty="无标签" variant="outline" />
      </div>
      <TextBlock label="描述" hint="description" text={data.description} />
      <TextBlock label="性格" hint="personality" text={data.personality} />
      <TextBlock label="场景" hint="scenario" text={data.scenario} />
      <TextBlock label="作者备注" hint="creator_notes" text={data.creator_notes} />
      {multilingual.map(([lang, text]) => (
        <TextBlock
          key={lang}
          label="作者备注"
          hint={`creator_notes_multilingual.${lang}`}
          text={String(text)}
        />
      ))}
    </div>
  );
}

function GreetingsTab({ data }: { data: CardDataV3 }) {
  const alternates = strings(data.alternate_greetings);
  const groupOnly = strings(data.group_only_greetings);
  return (
    <div className="space-y-4">
      <TextBlock label="首条开场白" hint="first_mes" text={data.first_mes} />
      {alternates.map((text, index) => (
        <TextBlock
          key={`alt-${index}`}
          label={`备选开场白 ${index + 1}`}
          hint={`alternate_greetings[${index}]`}
          text={text}
        />
      ))}
      {groupOnly.map((text, index) => (
        <TextBlock
          key={`group-${index}`}
          label={`群聊开场白 ${index + 1}`}
          hint={`group_only_greetings[${index}]`}
          text={text}
          badge={<Badge variant="success">群聊专用</Badge>}
        />
      ))}
      {!alternates.length && !groupOnly.length ? (
        <p className="text-xs text-slate-400">没有备选开场白或群聊专用开场白。</p>
      ) : null}
    </div>
  );
}

function PromptsTab({ data }: { data: CardDataV3 }) {
  const examples = exampleBlocks(data.mes_example ?? "");
  return (
    <div className="space-y-4">
      <TextBlock label="系统提示词" hint="system_prompt" text={data.system_prompt} />
      <TextBlock
        label="历史后指令"
        hint="post_history_instructions"
        text={data.post_history_instructions}
      />
      {examples.length ? (
        examples.map((text, index) => (
          <TextBlock key={index} label={`对话示例 ${index + 1}`} hint="mes_example" text={text} />
        ))
      ) : (
        <TextBlock label="对话示例" hint="mes_example" text="" />
      )}
    </div>
  );
}

function LoreEntryCard({ entry, index }: { entry: LoreEntryV3; index: number }) {
  const keys = strings(entry.keys);
  const secondary = strings(entry.secondary_keys);
  const enabled = entry.enabled !== false;
  const title = entry.name || entry.comment || keys[0] || `条目 ${index + 1}`;
  const flags: Array<[boolean | undefined, string]> = [
    [entry.constant, "常驻 constant"],
    [entry.selective, "选择性 selective"],
    [entry.use_regex, "正则 use_regex"],
    [entry.case_sensitive, "区分大小写"],
  ];
  return (
    <div className={cn("space-y-2.5 rounded-xl border border-blue-100 p-3.5", !enabled && "opacity-60")}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] text-slate-400">#{entry.id ?? index}</span>
        <h4 className="min-w-0 truncate text-sm font-semibold text-slate-800">{title}</h4>
        {enabled ? <Badge variant="success">启用</Badge> : <Badge variant="muted">停用</Badge>}
        {flags
          .filter(([on]) => on)
          .map(([, label]) => (
            <Badge key={label} variant="outline" className="text-[10px]">
              {label}
            </Badge>
          ))}
        <span className="ml-auto text-[10px] text-slate-400">
          顺序 {entry.insertion_order ?? 0}
          {entry.priority != null ? ` · 优先级 ${entry.priority}` : ""}
          {entry.position ? ` · ${entry.position}` : ""}
        </span>
      </div>
      <div className="grid gap-2 sm:grid-cols-[auto_1fr] sm:items-baseline">
        <span className="text-[11px] text-slate-500">关键词 keys</span>
        <Chips items={keys} empty={entry.constant ? "常驻条目，无需关键词" : "无关键词"} />
        {secondary.length ? (
          <>
            <span className="text-[11px] text-slate-500">次关键词</span>
            <Chips items={secondary} empty="" variant="outline" />
          </>
        ) : null}
      </div>
      <div className="whitespace-pre-wrap break-words rounded-lg bg-slate-50/70 px-3 py-2 text-sm leading-relaxed text-slate-700">
        <MacroText text={(entry.content ?? "").trim() || "（空内容）"} />
      </div>
    </div>
  );
}

function LorebookTab({ data }: { data: CardDataV3 }) {
  const book = data.character_book ?? {};
  const entries = (book.entries ?? [])
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => (a.entry.insertion_order ?? 0) - (b.entry.insertion_order ?? 0));
  const enabled = entries.filter(({ entry }) => entry.enabled !== false).length;
  return (
    <div className="space-y-4">
      <MetaGrid
        rows={[
          ["名称 name", book.name ?? ""],
          ["条目", `${formatNumber(entries.length)}（启用 ${formatNumber(enabled)}）`],
          ["扫描深度 scan_depth", book.scan_depth != null ? String(book.scan_depth) : ""],
          ["Token 预算 token_budget", book.token_budget != null ? formatNumber(book.token_budget) : ""],
          ["递归扫描", book.recursive_scanning == null ? "" : book.recursive_scanning ? "是" : "否"],
        ]}
      />
      {book.description ? (
        <TextBlock label="世界书说明" hint="description" text={book.description} />
      ) : null}
      {entries.length ? (
        entries.map(({ entry, index }) => <LoreEntryCard key={index} entry={entry} index={index} />)
      ) : (
        <EmptyState icon={BookOpen} text="这张卡没有内置世界书。" hint="character_book.entries 为空" />
      )}
    </div>
  );
}

function MetaTab({ detail, data }: { detail: CardDetail; data: CardDataV3 }) {
  const assets = data.assets ?? [];
  const sources = strings(data.source);
  const imported = detail.imported_from;
  return (
    <div className="space-y-4">
      <MetaGrid
        rows={[
          ["卡库 ID", `#${detail.id}`],
          ["规范", `${detail.spec} ${detail.spec_version}`],
          [
            "导入格式",
            imported.spec
              ? `${imported.spec}${imported.spec_version ? ` ${imported.spec_version}` : ""}`
              : "",
          ],
          ["来源类型", detail.source_kind],
          ["来源", detail.source, true],
          ["导入者", detail.installed_by],
          ["导入时间", formatTs(detail.created_at)],
          ["更新时间", formatTs(detail.updated_at)],
          ["指纹", detail.fingerprint, true],
        ]}
      />
      <div className="space-y-1.5">
        <h3 className="text-xs font-semibold text-slate-700">别名（卡库）</h3>
        <Chips items={detail.aliases} empty="无别名" />
      </div>
      <div className="space-y-1.5">
        <h3 className="text-xs font-semibold text-slate-700">
          来源链接 <span className="font-mono text-[10px] font-normal text-slate-400">source</span>
        </h3>
        {sources.length ? (
          <ul className="space-y-1 font-mono text-xs text-slate-600">
            {sources.map((item) => (
              <li key={item} className="break-all">
                {item}
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-xs text-muted-foreground">未填写</span>
        )}
      </div>
      <div className="space-y-1.5">
        <h3 className="text-xs font-semibold text-slate-700">
          资产 <span className="font-mono text-[10px] font-normal text-slate-400">assets</span>
        </h3>
        {assets.length ? (
          <div className="overflow-x-auto rounded-lg border border-blue-50">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-3 py-1.5 font-medium">type</th>
                  <th className="px-3 py-1.5 font-medium">name</th>
                  <th className="px-3 py-1.5 font-medium">ext</th>
                  <th className="px-3 py-1.5 font-medium">uri</th>
                </tr>
              </thead>
              <tbody>
                {assets.map((asset, index) => (
                  <tr key={index} className="border-t border-blue-50">
                    <td className="px-3 py-1.5">{asset.type || "-"}</td>
                    <td className="px-3 py-1.5">{asset.name || "-"}</td>
                    <td className="px-3 py-1.5">{asset.ext || "-"}</td>
                    <td
                      className="max-w-[16rem] truncate px-3 py-1.5 font-mono text-slate-500"
                      title={asset.uri}
                    >
                      {asset.uri || "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">无资产</span>
        )}
      </div>
      <div className="space-y-1.5">
        <h3 className="text-xs font-semibold text-slate-700">
          扩展 <span className="font-mono text-[10px] font-normal text-slate-400">extensions</span>
        </h3>
        {isEmptyObject(data.extensions) ? (
          <span className="text-xs text-muted-foreground">无扩展字段</span>
        ) : (
          <JsonBlock value={data.extensions} />
        )}
      </div>
    </div>
  );
}

function CopyJsonButton({ value }: { value: unknown }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => {
        navigator.clipboard
          ?.writeText(JSON.stringify(value, null, 2))
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          })
          .catch(() => undefined);
      }}
    >
      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      {copied ? "已复制" : "复制 JSON"}
    </Button>
  );
}

export function CardDetailModal({
  cardId,
  onClose,
}: {
  cardId: number | null;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const { data: detail, loading, error } = useApi<CardDetail>(
    cardId == null ? "" : `/api/cards/${cardId}`
  );
  // Ignore a stale payload while the next card is still loading.
  const current = detail && detail.id === cardId ? detail : null;
  const data = useMemo<CardDataV3>(() => current?.data ?? {}, [current]);
  const card = current ? { spec: current.spec, spec_version: current.spec_version, data } : null;

  const greetings =
    1 + strings(data.alternate_greetings).length + strings(data.group_only_greetings).length;
  const entries = data.character_book?.entries?.length ?? 0;
  const options: Array<{ value: Tab; label: string }> = [
    { value: "overview", label: "概览" },
    { value: "greetings", label: `开场白 ${greetings}` },
    { value: "prompts", label: "提示词" },
    { value: "lorebook", label: `世界书 ${entries}` },
    { value: "meta", label: "元数据" },
    { value: "json", label: "JSON" },
  ];

  const description = current
    ? `#${current.id} · ${current.spec} ${current.spec_version}` +
      (current.imported_from.spec ? ` · 导入自 ${current.imported_from.spec}` : "")
    : loading
      ? "加载中…"
      : undefined;

  const close = () => {
    setTab("overview");
    onClose();
  };

  return (
    <Modal
      open={cardId != null}
      title={current ? current.nickname || current.name : "角色卡详情"}
      description={description}
      onClose={close}
      wide
      panelClassName="max-w-4xl"
      footer={
        current && card ? (
          <>
            {current.is_active ? <Badge className="mr-auto">当前角色卡</Badge> : null}
            <CopyJsonButton value={card} />
            <Button size="sm" onClick={close}>
              关闭
            </Button>
          </>
        ) : null
      }
    >
      {error ? <ErrorBanner message={error} /> : null}
      {current && card ? (
        <div className="space-y-4">
          <PillTabs value={tab} options={options} onChange={setTab} className="flex-wrap" />
          {tab === "overview" ? <OverviewTab detail={current} data={data} /> : null}
          {tab === "greetings" ? <GreetingsTab data={data} /> : null}
          {tab === "prompts" ? <PromptsTab data={data} /> : null}
          {tab === "lorebook" ? <LorebookTab data={data} /> : null}
          {tab === "meta" ? <MetaTab detail={current} data={data} /> : null}
          {tab === "json" ? <JsonBlock value={card} /> : null}
        </div>
      ) : !error ? (
        <p className="py-8 text-center text-sm text-slate-400">加载中…</p>
      ) : null}
    </Modal>
  );
}

import { useState } from "react";
import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, Trash2 } from "lucide-react";

import { Modal } from "@/components/models/Modal";
import { CheckboxRow } from "@/components/ui/form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { StickerRow } from "@/lib/analytics";
import { formatBytes, formatNumber, formatTs } from "@/lib/format";

import { StickerThumb, stickerImageUrl } from "./StickerThumb";

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="shrink-0 text-xs text-slate-500">{label}</dt>
      <dd className="min-w-0 truncate text-right text-xs tabular-nums text-slate-700">
        {children}
      </dd>
    </div>
  );
}

export function StickerDetailModal({
  sticker,
  refreshKey = 0,
  deleting = false,
  onDelete,
  onClose,
}: {
  sticker: StickerRow | null;
  refreshKey?: number;
  deleting?: boolean;
  onDelete: (sticker: StickerRow, blockSimilar: boolean) => void;
  onClose: () => void;
}) {
  const [blockSimilar, setBlockSimilar] = useState(true);
  if (!sticker) return null;
  const banned = sticker.status === "banned";

  return (
    <Modal
      open
      wide
      title={`表情 #${sticker.id}`}
      description={sticker.description || "还没有打标"}
      onClose={onClose}
      footer={
        <>
          <CheckboxRow
            label="同时屏蔽相似图片"
            hint="删除同款并阻止以后入库"
            checked={blockSimilar}
            onChange={setBlockSimilar}
            disabled={deleting}
          />
          <Button
            variant="destructive"
            onClick={() => onDelete(sticker, blockSimilar)}
            disabled={deleting}
          >
            <Trash2 className="h-4 w-4" />
            {deleting ? "删除中…" : "删除表情"}
          </Button>
        </>
      }
    >
      <div className="grid gap-5 sm:grid-cols-[minmax(0,1fr)_16rem]">
        <div className="flex min-h-[14rem] items-center justify-center overflow-hidden rounded-xl border border-slate-200 bg-slate-50 p-4">
          <StickerThumb
            id={sticker.id}
            hasImage={sticker.has_image}
            alt={sticker.description || `表情 #${sticker.id}`}
            thumb={false}
            className="max-h-[52vh] max-w-full object-contain drop-shadow-sm"
            iconClassName="h-10 w-10"
            placeholderClassName="h-40 w-40"
            refreshKey={refreshKey}
          />
        </div>

        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {banned ? (
              <Badge variant="destructive">
                <AlertTriangle className="mr-1 h-3 w-3" /> 已禁用
              </Badge>
            ) : (
              <Badge variant="success">
                <CheckCircle2 className="mr-1 h-3 w-3" /> 可用
              </Badge>
            )}
            <Badge variant="outline" className="font-mono text-[10px]">
              {sticker.mime || "unknown"}
            </Badge>
          </div>

          <div className="flex flex-wrap gap-1.5">
            {sticker.tags.length ? (
              sticker.tags.map((tag) => (
                <Badge key={tag} variant="secondary" className="text-[10px]">
                  {tag}
                </Badge>
              ))
            ) : (
              <span className="text-xs text-slate-500">没有标签</span>
            )}
          </div>

          <dl className="divide-y divide-slate-100 rounded-xl border border-slate-200 px-3">
            <MetaRow label="使用次数">{formatNumber(sticker.uses)}</MetaRow>
            <MetaRow label="文件大小">{formatBytes(sticker.size)}</MetaRow>
            <MetaRow label="来源">{sticker.source || "-"}</MetaRow>
            <MetaRow label="来源类型">{sticker.source_kind || "-"}</MetaRow>
            <MetaRow label="加入时间">{formatTs(sticker.created_at)}</MetaRow>
            <MetaRow label="最后使用">
              {sticker.last_used ? formatTs(sticker.last_used) : "-"}
            </MetaRow>
          </dl>

          {sticker.has_image ? (
            <a
              href={stickerImageUrl(sticker.id)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 hover:underline"
            >
              <ExternalLink className="h-3 w-3" /> 在新标签打开原图
            </a>
          ) : (
            <Badge variant="warning">图片文件已不在表情库目录里</Badge>
          )}
        </div>
      </div>
    </Modal>
  );
}

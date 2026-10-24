import { useState } from "react";
import { ImageOff } from "lucide-react";

import { cn } from "@/lib/utils";

export function stickerImageUrl(id: number, thumb = false) {
  return `/api/stickers/${id}/image${thumb ? "?thumb=1" : ""}`;
}

/** One sticker, with a file-missing / load-failure placeholder. */
export function StickerThumb({
  id,
  hasImage,
  alt,
  thumb = true,
  className,
  iconClassName,
  placeholderClassName,
  refreshKey = 0,
}: {
  id: number;
  hasImage: boolean;
  alt: string;
  thumb?: boolean;
  className?: string;
  iconClassName?: string;
  placeholderClassName?: string;
  refreshKey?: number;
}) {
  // Remember which poll the failure belongs to, so a later refresh retries
  // instead of leaving the tile broken until the page is reloaded.
  const [failure, setFailure] = useState<{ id: number; refresh: number } | null>(null);
  const failed = failure?.id === id && failure.refresh === refreshKey;

  if (!hasImage || failed) {
    return (
      <div
        className={cn(
          "flex items-center justify-center bg-slate-50 text-slate-300",
          className,
          placeholderClassName
        )}
      >
        <ImageOff className={cn("h-6 w-6", iconClassName)} />
      </div>
    );
  }

  return (
    <img
      src={stickerImageUrl(id, thumb)}
      alt={alt}
      loading="lazy"
      decoding="async"
      draggable={false}
      onError={() => setFailure({ id, refresh: refreshKey })}
      className={className}
    />
  );
}

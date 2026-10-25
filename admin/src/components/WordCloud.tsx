import { useMemo } from "react";

export type CloudWord = {
  text: string;
  count: number;
};

type PlacedWord = CloudWord & {
  x: number;
  y: number;
  fontSize: number;
  fontWeight: number;
  fill: string;
  opacity: number;
};

type Box = {
  left: number;
  right: number;
  top: number;
  bottom: number;
};

const WIDTH = 900;
const HEIGHT = 420;
const FONT_FAMILY =
  'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
const COLORS = ["#1d4ed8", "#2563eb", "#0284c7", "#4f46e5", "#0891b2", "#3b82f6"];

function hashSeed(seed: string): number {
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function estimateWidth(text: string, fontSize: number): number {
  let width = 0;
  for (const char of text) {
    width += /[　-鿿＀-￯]/.test(char) ? fontSize : fontSize * 0.6;
  }
  return width;
}

function overlaps(a: Box, b: Box, padding: number): boolean {
  return !(
    a.right + padding <= b.left ||
    b.right + padding <= a.left ||
    a.bottom + padding <= b.top ||
    b.bottom + padding <= a.top
  );
}

type RankedWord = {
  word: CloudWord;
  ratio: number;
};

function rankedWords(words: CloudWord[]): RankedWord[] {
  const sorted = [...words].sort(
    (a, b) => b.count - a.count || a.text.localeCompare(b.text)
  );
  const counts = sorted.map((word) => word.count);
  const minCount = Math.min(...counts);
  const maxCount = Math.max(...counts);
  const spread = maxCount - minCount;
  return sorted.map((word, index) => ({
    word,
    // With flat counts fall back to rank so the cloud still has a focal word.
    ratio:
      spread > 0
        ? (word.count - minCount) / spread
        : 1 - index / Math.max(1, sorted.length - 1),
  }));
}

/** Place the largest words near the middle along a deterministic spiral. */
function layoutWords(words: CloudWord[], seed: string): PlacedWord[] {
  if (!words.length) return [];
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const random = mulberry32(hashSeed(seed));
  const boxes: Box[] = [];
  const placed: PlacedWord[] = [];
  const centerX = WIDTH / 2;
  const centerY = HEIGHT / 2;

  rankedWords(words).forEach(({ word, ratio }, index) => {
    const fontSize = 13 + Math.round(33 * Math.pow(ratio, 0.6));
    const fontWeight = index < 3 ? 700 : index < 12 ? 600 : 500;
    const fill = COLORS[index % COLORS.length];
    const opacity = 0.72 + 0.28 * ratio;
    let textWidth = estimateWidth(word.text, fontSize);
    if (context !== null) {
      context.font = `${fontWeight} ${fontSize}px ${FONT_FAMILY}`;
      textWidth = context.measureText(word.text).width;
    }
    const halfWidth = textWidth / 2 + 6;
    const halfHeight = fontSize * 0.62;
    const startAngle = index === 0 ? 0 : random() * Math.PI * 2;

    for (let step = 0; step < 1200; step += 1) {
      const angle = startAngle + step * 0.17;
      const radius = step * 0.42;
      const x = centerX + Math.cos(angle) * radius * 1.55;
      const y = centerY + Math.sin(angle) * radius * 0.7;
      const box = {
        left: x - halfWidth,
        right: x + halfWidth,
        top: y - halfHeight,
        bottom: y + halfHeight,
      };
      if (
        box.left < 4 ||
        box.right > WIDTH - 4 ||
        box.top < 4 ||
        box.bottom > HEIGHT - 4
      ) {
        continue;
      }
      if (boxes.some((other) => overlaps(box, other, 4))) {
        continue;
      }
      boxes.push(box);
      placed.push({
        ...word,
        x,
        y,
        fontSize,
        fontWeight,
        fill,
        opacity,
      });
      break;
    }
  });

  return placed;
}

function WordChips({ words }: { words: CloudWord[] }) {
  const items = rankedWords(words).slice(0, 24);
  if (!items.length) return null;
  return (
    <div className="flex min-h-[220px] flex-wrap content-center items-baseline justify-center gap-x-3 gap-y-1.5 px-1 py-2">
      {items.map(({ word, ratio }, index) => (
        <span
          key={word.text}
          title={`${word.text} · ${word.count} 条消息`}
          style={{
            fontSize: `${14 + Math.round(16 * Math.pow(ratio, 0.6))}px`,
            color: COLORS[index % COLORS.length],
            fontWeight: index < 3 ? 700 : index < 10 ? 600 : 500,
            opacity: 0.72 + 0.28 * ratio,
            lineHeight: 1.1,
          }}
        >
          {word.text}
        </span>
      ))}
    </div>
  );
}

export default function WordCloud({
  words,
  seed,
  compact = false,
}: {
  words: CloudWord[];
  seed: string;
  compact?: boolean;
}) {
  const placed = useMemo(
    () => (compact ? [] : layoutWords(words, seed)),
    [compact, words, seed]
  );
  if (compact) return <WordChips words={words} />;
  if (!placed.length) return null;

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label="聊天词云"
      className="h-auto w-full select-none"
    >
      {placed.map((word) => (
        <text
          key={word.text}
          x={word.x}
          y={word.y}
          textAnchor="middle"
          dominantBaseline="central"
          fill={word.fill}
          opacity={word.opacity}
          fontSize={word.fontSize}
          fontWeight={word.fontWeight}
          style={{ fontFamily: FONT_FAMILY }}
        >
          <title>{`${word.text} · ${word.count} 条消息`}</title>
          {word.text}
        </text>
      ))}
    </svg>
  );
}

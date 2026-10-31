import { useMemo, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import { BarChart3, Table2 } from "lucide-react";

import { EmptyState, SectionCard } from "@/components/common/Bits";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatCompact, formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

export type TrendPoint = { day: string; value: number };

const WIDTH = 680;
const HEIGHT = 220;
const PAD = { top: 18, right: 20, bottom: 30, left: 52 };
const PLOT_W = WIDTH - PAD.left - PAD.right;
const PLOT_H = HEIGHT - PAD.top - PAD.bottom;

function niceMax(value: number) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  const fraction = value / base;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return nice * base;
}

function tickIndices(length: number) {
  if (length <= 8) return Array.from({ length }, (_, index) => index);
  const count = 6;
  const step = (length - 1) / (count - 1);
  return [...new Set(Array.from({ length: count }, (_, index) => Math.round(index * step)))];
}

function dayLabel(day: string) {
  return day.length >= 10 ? day.slice(5) : day;
}

export default function TrendChart({
  title,
  description,
  data,
  color = "var(--chart-2)",
  formatValue = formatNumber,
  unit = "",
}: {
  title: string;
  description?: string;
  data: TrendPoint[];
  color?: string;
  formatValue?: (value: number) => string;
  unit?: string;
}) {
  const [mode, setMode] = useState<"chart" | "table">("chart");
  const [hover, setHover] = useState<number | null>(null);

  const values = useMemo(() => data.map((point) => point.value), [data]);
  const max = useMemo(() => niceMax(Math.max(0, ...values)), [values]);
  const last = data.length ? data[data.length - 1] : null;

  const xFor = (index: number) =>
    PAD.left + (data.length <= 1 ? PLOT_W / 2 : (index / (data.length - 1)) * PLOT_W);
  const yFor = (value: number) => PAD.top + PLOT_H - (value / max) * PLOT_H;

  const line = data
    .map((point, index) => `${index ? "L" : "M"}${xFor(index).toFixed(2)},${yFor(point.value).toFixed(2)}`)
    .join(" ");
  const area = data.length
    ? `${line} L${xFor(data.length - 1).toFixed(2)},${(PAD.top + PLOT_H).toFixed(2)} L${xFor(0).toFixed(2)},${(PAD.top + PLOT_H).toFixed(2)} Z`
    : "";

  const active = hover !== null ? data[hover] : null;

  function handleMove(event: PointerEvent<SVGRectElement>) {
    if (!data.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width)));
    setHover(Math.round(ratio * (data.length - 1)));
  }

  function handleKey(event: KeyboardEvent<SVGSVGElement>) {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      setHover((current) => {
        const base = current ?? data.length - 1;
        const next = event.key === "ArrowLeft" ? base - 1 : base + 1;
        return Math.max(0, Math.min(data.length - 1, next));
      });
    } else if (event.key === "Escape") {
      setHover(null);
    }
  }

  return (
    <SectionCard
      title={title}
      description={description}
      contentClassName="p-4"
      actions={
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold tabular-nums text-slate-900">
            {last ? `${formatValue(last.value)}${unit}` : "-"}
          </span>
          <div className="flex rounded-lg border border-slate-200 bg-white p-0.5">
            <button
              type="button"
              onClick={() => setMode("chart")}
              aria-pressed={mode === "chart"}
              title="图表"
              className={cn(
                "inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors",
                mode === "chart"
                  ? "bg-primary text-white shadow-sm"
                  : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
              )}
            >
              <BarChart3 className="h-3.5 w-3.5" />
              图表
            </button>
            <button
              type="button"
              onClick={() => setMode("table")}
              aria-pressed={mode === "table"}
              title="表格"
              className={cn(
                "inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors",
                mode === "table"
                  ? "bg-primary text-white shadow-sm"
                  : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
              )}
            >
              <Table2 className="h-3.5 w-3.5" />
              表格
            </button>
          </div>
        </div>
      }
    >
      {!data.length ? (
        <EmptyState icon={BarChart3} text="还没有数据" />
      ) : mode === "table" ? (
        <div className="max-h-[248px] overflow-auto rounded-xl border border-slate-200">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>日期</TableHead>
                <TableHead className="text-right">{title}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((point) => (
                <TableRow key={point.day}>
                  <TableCell className="whitespace-nowrap text-xs tabular-nums text-slate-600">
                    {point.day}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums text-slate-800">
                    {formatValue(point.value)}
                    {unit}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : (
        <div className="relative">
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            className="h-auto w-full rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
            role="img"
            aria-label={`${title}趋势，${data[0]?.day} 至 ${last?.day}`}
            tabIndex={0}
            onKeyDown={handleKey}
            onBlur={() => setHover(null)}
          >
            <title>{`${title}：${data[0]?.day} 至 ${last?.day}`}</title>
            {[0, 0.5, 1].map((ratio) => {
              const value = max * ratio;
              const y = yFor(value);
              return (
                <g key={ratio}>
                  <line
                    x1={PAD.left}
                    x2={PAD.left + PLOT_W}
                    y1={y}
                    y2={y}
                    stroke="#e2e8f0"
                    strokeWidth={1}
                  />
                  <text
                    x={PAD.left - 10}
                    y={y + 3.5}
                    textAnchor="end"
                    className="fill-slate-400 tabular-nums"
                    fontSize={10}
                  >
                    {formatCompact(value)}
                  </text>
                </g>
              );
            })}

            <path d={area} fill={color} fillOpacity={0.1} />
            <path
              d={line}
              fill="none"
              stroke={color}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            />

            {tickIndices(data.length).map((index) => (
              <text
                key={data[index].day}
                x={xFor(index)}
                y={HEIGHT - 8}
                textAnchor="middle"
                className="fill-slate-400 tabular-nums"
                fontSize={10}
              >
                {dayLabel(data[index].day)}
              </text>
            ))}

            {last ? (
              <circle
                cx={xFor(data.length - 1)}
                cy={yFor(last.value)}
                r={4}
                fill={color}
                stroke="#ffffff"
                strokeWidth={2}
              />
            ) : null}

            {active && hover !== null ? (
              <>
                <line
                  x1={xFor(hover)}
                  x2={xFor(hover)}
                  y1={PAD.top}
                  y2={PAD.top + PLOT_H}
                  stroke="#94a3b8"
                  strokeWidth={1}
                />
                <circle
                  cx={xFor(hover)}
                  cy={yFor(active.value)}
                  r={4}
                  fill={color}
                  stroke="#ffffff"
                  strokeWidth={2}
                />
              </>
            ) : null}

            <rect
              x={PAD.left}
              y={PAD.top}
              width={PLOT_W}
              height={PLOT_H}
              fill="transparent"
              onPointerMove={handleMove}
              onPointerLeave={() => setHover(null)}
            />
          </svg>

          {active && hover !== null ? (
            <div
              className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-[115%] rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs shadow-md"
              style={{
                left: `${Math.min(92, Math.max(8, (xFor(hover) / WIDTH) * 100))}%`,
                top: `${(yFor(active.value) / HEIGHT) * 100}%`,
              }}
            >
              <div className="text-[10px] text-slate-500">{active.day}</div>
              <div className="font-semibold tabular-nums text-slate-900">
                {formatValue(active.value)}
                {unit}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </SectionCard>
  );
}

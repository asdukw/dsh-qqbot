import type { TaskRow } from "@/lib/analytics";

export const numberFmt = new Intl.NumberFormat("zh-CN");

export function formatNumber(value: number | null | undefined) {
  return numberFmt.format(Number(value ?? 0));
}

export function formatCompact(value: number | null | undefined) {
  const number = Number(value ?? 0);
  if (!Number.isFinite(number)) return "0";
  const abs = Math.abs(number);
  if (abs >= 1_000_000_000) return `${(number / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${(number / 1_000).toFixed(0)}K`;
  if (abs >= 1_000) return `${(number / 1_000).toFixed(1)}K`;
  return numberFmt.format(number);
}

export function formatPercent(value: number | null | undefined) {
  return `${((Number(value ?? 0)) * 100).toFixed(1)}%`;
}

export function formatTs(value: number | null | undefined) {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatDate(value: number | null | undefined) {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

export function formatClock(value: number | null | undefined) {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

export function formatDuration(seconds: number | null | undefined) {
  const total = Math.max(0, Math.floor(Number(seconds ?? 0)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分钟`;
  return `${total} 秒`;
}

export function formatBytes(value: number | null | undefined) {
  const bytes = Number(value ?? 0);
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function weekdayLabel(value: number | null | undefined) {
  if (value == null) return "";
  return ["一", "二", "三", "四", "五", "六", "日"][value] ?? String(value);
}

export function scheduleText(task: TaskRow) {
  const time = task.time_of_day || "";
  switch (task.kind) {
    case "daily":
      return `每天 ${time}`.trim();
    case "weekly":
      return `每周${weekdayLabel(task.weekday)} ${time}`.trim();
    case "interval":
      return `每 ${task.every_minutes ?? "?"} 分钟`;
    case "once":
      return `一次 ${time || formatTs(task.next_run)}`.trim();
    default:
      return task.kind;
  }
}

export function targetLabel(chatType: string, chatId: number | string) {
  return `${chatType === "group" ? "群" : "好友"} ${chatId}`;
}

export function taskModeLabel(mode: string) {
  if (mode === "generate") return "现写文案";
  if (mode === "fixed") return "固定文案";
  return mode || "-";
}

export function conversationLabel(id: string, fallback = "") {
  if (id.startsWith("group:")) return `群 ${id.slice(6)}`;
  if (id.startsWith("private:")) return `好友 ${id.slice(8)}`;
  if (id.startsWith("task:")) return `任务 ${id.slice(5)}`;
  return fallback || id || "未知会话";
}

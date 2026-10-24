export type UsageDay = {
  day: string;
  input: number;
  cache_read: number;
  output: number;
  total: number;
  events: number;
};

export type UsageBucket = UsageDay & { purpose: string };

export type UsageSession = {
  conversation_id: string;
  session_id: string;
  input: number;
  cache_read: number;
  output: number;
  total: number;
  events: number;
};

export type UsageConversation = {
  id: string;
  label: string;
  input: number;
  cache_read: number;
  output: number;
  total: number;
};

export type UsageData = {
  days: number;
  conversation_id: string;
  totals: {
    input: number;
    cache_read: number;
    output: number;
    total: number;
    events: number;
    cache_hit_rate: number;
  };
  by_day: UsageDay[];
  by_purpose: UsageBucket[];
  by_session: UsageSession[];
  conversations: UsageConversation[];
  recent: Array<{
    created_at: number;
    source: string;
    purpose: string;
    conversation_id: string;
    session_id: string;
    provider: string;
    model: string;
    input: number;
    cache_read: number;
    output: number;
    total: number;
  }>;
};

export type FunData = {
  date: string;
  conversation_id: string;
  messages: number;
  ranking: Array<{ user_id: number; name: string; messages: number }>;
  words: Array<{ text: string; count: number }>;
  conversations: Array<{ id: string; label: string; messages: number }>;
};

export const numberFmt = new Intl.NumberFormat("zh-CN");

export function formatNumber(value: number | null | undefined) {
  return numberFmt.format(Number(value ?? 0));
}

export function formatPercent(value: number | null | undefined) {
  return `${((Number(value ?? 0)) * 100).toFixed(1)}%`;
}

export function conversationLabel(id: string, fallback = "") {
  if (id.startsWith("group:")) return `群 ${id.slice(6)}`;
  if (id.startsWith("private:")) return `好友 ${id.slice(8)}`;
  if (id.startsWith("task:")) return `任务 ${id.slice(5)}`;
  return fallback || id || "未知会话";
}

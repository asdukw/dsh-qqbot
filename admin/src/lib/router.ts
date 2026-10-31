import { useCallback, useEffect, useState } from "react";
import type { ComponentType } from "react";
import {
  Activity,
  AudioLines,
  BookOpen,
  CalendarClock,
  CreditCard,
  LayoutDashboard,
  Rss,
  Server,
  Settings2,
  Sparkles,
  Sticker,
  UserCheck,
} from "lucide-react";

export type RouteId =
  | "overview"
  | "insights"
  | "requests"
  | "tasks"
  | "feeds"
  | "models"
  | "story"
  | "cards"
  | "stickers"
  | "voice"
  | "usage"
  | "system"
  | "video-credentials";

export type RouteMeta = {
  id: RouteId;
  label: string;
  description: string;
  icon: ComponentType<{ className?: string }>;
};

export type NavGroup = {
  label: string;
  items: RouteMeta[];
};

export const ROUTES: RouteMeta[] = [
  {
    id: "overview",
    label: "仪表盘",
    description: "运行状态、趋势与需要留意的问题",
    icon: LayoutDashboard,
  },
  {
    id: "insights",
    label: "聊天分析",
    description: "发言排行、会话分布与词云",
    icon: Sparkles,
  },
  {
    id: "requests",
    label: "审批",
    description: "好友请求、群邀请与入群申请",
    icon: UserCheck,
  },
  {
    id: "tasks",
    label: "定时任务",
    description: "定时消息与运行记录",
    icon: CalendarClock,
  },
  {
    id: "feeds",
    label: "订阅推送",
    description: "B 站动态与 RSS / Atom 订阅",
    icon: Rss,
  },
  {
    id: "models",
    label: "模型",
    description: "供应商、模型注册表与当前 route",
    icon: Server,
  },
  { id: "story", label: "剧情与关系", description: "剧情存档、世界包与生成状态", icon: BookOpen },
  {
    id: "cards",
    label: "角色卡",
    description: "人格库、别名与来源",
    icon: CreditCard,
  },
  {
    id: "stickers",
    label: "表情库",
    description: "梗图素材、打标与使用情况",
    icon: Sticker,
  },
  {
    id: "voice",
    label: "音色",
    description: "TTS 音色试听与在线切换",
    icon: AudioLines,
  },
  {
    id: "usage",
    label: "Token 用量",
    description: "主会话与辅助调用的 token 账本",
    icon: Activity,
  },
  { id: "video-credentials", label: "视频凭据", description: "B 站与抖音 Cookie 管理", icon: Settings2 },
  {
    id: "system",
    label: "系统状态",
    description: "数据库、路径与面板配置",
    icon: Settings2,
  },
];

const ROUTE_MAP = new Map(ROUTES.map((route) => [route.id, route]));

export const NAV_GROUPS: NavGroup[] = [
  { label: "概览", items: [ROUTE_MAP.get("overview")!, ROUTE_MAP.get("insights")!] },
  { label: "自动化", items: [ROUTE_MAP.get("requests")!, ROUTE_MAP.get("tasks")!, ROUTE_MAP.get("feeds")!] },
  { label: "内容", items: [ROUTE_MAP.get("models")!, ROUTE_MAP.get("cards")!, ROUTE_MAP.get("story")!, ROUTE_MAP.get("stickers")!, ROUTE_MAP.get("voice")!] },
  { label: "用量", items: [ROUTE_MAP.get("usage")!] },
  { label: "系统", items: [ROUTE_MAP.get("system")!, ROUTE_MAP.get("video-credentials")!] },
];

export function routeMeta(id: RouteId): RouteMeta {
  return ROUTE_MAP.get(id) ?? ROUTES[0];
}

function readRoute(): RouteId {
  const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return (ROUTE_MAP.has(raw as RouteId) ? raw : "overview") as RouteId;
}

export function useHashRoute() {
  const [route, setRoute] = useState<RouteId>(readRoute);

  useEffect(() => {
    const onChange = () => setRoute(readRoute());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = useCallback((id: RouteId) => {
    if (window.location.hash === `#/${id}`) {
      setRoute(id);
      return;
    }
    window.location.hash = `#/${id}`;
  }, []);

  return { route, navigate };
}

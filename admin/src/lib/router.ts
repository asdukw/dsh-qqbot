import { useCallback, useEffect, useState } from "react";
import type { ComponentType } from "react";
import {
  Activity,
  CalendarClock,
  CreditCard,
  LayoutDashboard,
  Rss,
  Server,
  Settings2,
  Sparkles,
  Sticker,
} from "lucide-react";

export type RouteId =
  | "overview"
  | "insights"
  | "tasks"
  | "feeds"
  | "models"
  | "cards"
  | "stickers"
  | "usage"
  | "system";

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
    description: "运行概览、今日趋势与需要关注的问题",
    icon: LayoutDashboard,
  },
  {
    id: "insights",
    label: "聊天分析",
    description: "今天的发言排行、词云与会话分布",
    icon: Sparkles,
  },
  {
    id: "tasks",
    label: "定时任务",
    description: "按群 / 私聊执行的定时消息与运行记录",
    icon: CalendarClock,
  },
  {
    id: "feeds",
    label: "订阅推送",
    description: "B 站动态与通用 RSS / Atom 订阅",
    icon: Rss,
  },
  {
    id: "models",
    label: "模型",
    description: "供应商、SQLite 模型注册表与当前 DSH route",
    icon: Server,
  },
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
    id: "usage",
    label: "Token 用量",
    description: "主会话与辅助调用的 token 账本",
    icon: Activity,
  },
  {
    id: "system",
    label: "系统状态",
    description: "数据库、路径与面板配置",
    icon: Settings2,
  },
];

const ROUTE_MAP = new Map(ROUTES.map((route) => [route.id, route]));

export const NAV_GROUPS: NavGroup[] = [
  { label: "概览", items: [ROUTE_MAP.get("overview")!] },
  { label: "对话", items: [ROUTE_MAP.get("insights")!] },
  { label: "自动化", items: [ROUTE_MAP.get("tasks")!, ROUTE_MAP.get("feeds")!] },
  { label: "内容", items: [ROUTE_MAP.get("models")!, ROUTE_MAP.get("cards")!, ROUTE_MAP.get("stickers")!] },
  { label: "用量", items: [ROUTE_MAP.get("usage")!] },
  { label: "系统", items: [ROUTE_MAP.get("system")!] },
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

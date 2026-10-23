import { useCallback, useEffect, useMemo, useState } from "react";
import type { ComponentType, FormEvent, ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  CalendarClock,
  CheckCircle2,
  Clock,
  CreditCard,
  Hash,
  LogOut,
  MessageSquare,
  PauseCircle,
  Radio,
  RefreshCw,
  Rss,
  Server,
  ShieldCheck,
  Smile,
  UserRound,
  Waves,
} from "lucide-react";

import FunPanel from "@/components/FunPanel";
import UsagePanel from "@/components/UsagePanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { conversationLabel, type FunData, type UsageData } from "@/lib/analytics";
import { cn } from "@/lib/utils";

type ModelRow = {
  provider: string;
  model_id: string;
  context_window: number;
  is_active: boolean;
  enabled: boolean;
};

type CardRow = {
  id: number;
  name: string;
  nickname: string;
  aliases: string[];
  is_active: boolean;
};

type TaskRow = {
  id: number;
  kind: string;
  time_of_day: string | null;
  weekday: number | null;
  every_minutes: number | null;
  mode: string;
  enabled: boolean;
  next_run: number | null;
  message: string;
};

type FeedRow = {
  id: number;
  chat_type: string;
  chat_id: number;
  title: string;
  feed_url: string;
  last_error: string;
};

type Dashboard = {
  generated_at: number;
  active_model: string;
  active_card: string;
  messages: { total: number; last_at: number | null };
  tasks: { total: number; enabled: number };
  feeds: { total: number };
  stickers: { total: number; banned: number };
  model_rows: ModelRow[];
  card_rows: CardRow[];
  task_rows: TaskRow[];
  feed_rows: FeedRow[];
};

class Unauthorized extends Error {}

const numberFmt = new Intl.NumberFormat("zh-CN");

function formatNumber(value: number | null | undefined) {
  return numberFmt.format(Number(value ?? 0));
}

function formatTs(value: number | null | undefined) {
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

function formatClock(value: number | null | undefined) {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function weekdayLabel(value: number | null | undefined) {
  if (value == null) return "";
  return ["一", "二", "三", "四", "五", "六", "日"][value] ?? String(value);
}

function scheduleText(task: TaskRow) {
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

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers || {}),
    },
  });
  if (response.status === 401) throw new Unauthorized();
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function LoginScreen({
  password,
  setPassword,
  busy,
  error,
  onSubmit,
}: {
  password: string;
  setPassword: (value: string) => void;
  busy: boolean;
  error: string;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <div className="relative min-h-screen overflow-hidden bg-[#f4f8ff]">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_18%_8%,rgba(59,130,246,0.18),transparent_34%),radial-gradient(circle_at_82%_92%,rgba(147,197,253,0.28),transparent_42%)]" />
      <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,rgba(37,99,235,0.06)_1px,transparent_1px),linear-gradient(to_bottom,rgba(37,99,235,0.06)_1px,transparent_1px)] [background-size:48px_48px] [mask-image:radial-gradient(ellipse_at_center,black,transparent_70%)]" />
      <div className="relative flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-sm border-blue-100 bg-white/95 shadow-soft backdrop-blur">
          <CardHeader className="items-center pb-2 text-center">
            <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-600 text-white shadow-lg shadow-blue-600/25">
              <Waves className="h-7 w-7" />
            </div>
            <CardTitle className="text-xl">大肥鱼 Admin</CardTitle>
            <CardDescription>QQ bot 运行面板 · 只读</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <form className="space-y-3" onSubmit={onSubmit}>
              <label className="text-sm font-medium text-slate-700" htmlFor="password">
                管理员密码
              </label>
              <Input
                id="password"
                type="password"
                autoFocus
                autoComplete="current-password"
                placeholder="ADMIN_UI_PASSWORD"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              {error ? (
                <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{error}</span>
                </div>
              ) : null}
              <Button className="w-full" type="submit" disabled={busy || !password}>
                {busy ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <ShieldCheck className="h-4 w-4" />
                )}
                {busy ? "登录中…" : "登录"}
              </Button>
            </form>
            <Separator />
            <p className="text-center text-xs text-muted-foreground">
              仅限本机 / 白名单访问 · 密码来自 bot.env
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card className="transition-shadow hover:shadow-md">
      <CardContent className="flex items-start justify-between p-5">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
          <p className="mt-2 truncate text-xl font-semibold text-slate-900" title={value}>
            {value}
          </p>
          {hint ? <p className="mt-1 truncate text-xs text-muted-foreground">{hint}</p> : null}
        </div>
        <div className="ml-3 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
          <Icon className="h-5 w-5" />
        </div>
      </CardContent>
    </Card>
  );
}

function SectionCard({
  icon: Icon,
  title,
  description,
  badge,
  children,
}: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  description?: string;
  badge?: string;
  children: ReactNode;
}) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row items-start justify-between space-y-0 border-b border-blue-50 pb-4">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
            <Icon className="h-4 w-4" />
          </div>
          <div>
            <CardTitle>{title}</CardTitle>
            {description ? <CardDescription className="mt-1">{description}</CardDescription> : null}
          </div>
        </div>
        {badge ? <Badge variant="secondary">{badge}</Badge> : null}
      </CardHeader>
      <CardContent className="p-0">{children}</CardContent>
    </Card>
  );
}

function EmptyState({ icon: Icon, text }: { icon: ComponentType<{ className?: string }>; text: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center text-sm text-muted-foreground">
      <Icon className="h-6 w-6 text-blue-200" />
      <p>{text}</p>
    </div>
  );
}

function DashboardView({
  data,
  refreshing,
  error,
  onRefresh,
  onLogout,
  usage,
  fun,
  insightsLoading,
  usageDays,
  conversation,
  conversationOptions,
  onUsageDaysChange,
  onConversationChange,
}: {
  data: Dashboard;
  refreshing: boolean;
  error: string;
  onRefresh: () => void;
  onLogout: () => void;
  usage: UsageData | null;
  fun: FunData | null;
  insightsLoading: boolean;
  usageDays: number;
  conversation: string;
  conversationOptions: Array<{ id: string; label: string }>;
  onUsageDaysChange: (days: number) => void;
  onConversationChange: (id: string) => void;
}) {
  const unavailableFeeds = useMemo(
    () => data.feed_rows.filter((feed) => Boolean(feed.last_error)).length,
    [data.feed_rows]
  );

  return (
    <div className="min-h-screen bg-[#f4f8ff]">
      <header className="sticky top-0 z-20 border-b border-blue-100 bg-white/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3 md:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-600 text-white shadow-md shadow-blue-600/20">
              <Waves className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-semibold tracking-tight text-slate-900">大肥鱼 Admin</h1>
                <Badge variant="secondary">只读</Badge>
              </div>
              <p className="text-xs text-muted-foreground">QQ bot 运行面板</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden text-xs text-muted-foreground sm:inline">
              更新于 {formatClock(data.generated_at)}
            </span>
            <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
              <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
              刷新
            </Button>
            <Button variant="ghost" size="sm" onClick={onLogout}>
              <LogOut className="h-4 w-4" />
              退出
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6 md:px-8 md:py-8">
        {error ? (
          <div className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          <StatCard icon={Bot} label="当前模型" value={data.active_model} hint="DSH route" />
          <StatCard icon={UserRound} label="当前角色卡" value={data.active_card} hint="active card" />
          <StatCard
            icon={MessageSquare}
            label="消息记录"
            value={formatNumber(data.messages.total)}
            hint={`最后 ${formatTs(data.messages.last_at)}`}
          />
          <StatCard
            icon={CalendarClock}
            label="定时任务"
            value={`${data.tasks.enabled}/${data.tasks.total}`}
            hint="启用 / 总数"
          />
          <StatCard
            icon={Rss}
            label="RSS 订阅"
            value={formatNumber(data.feeds.total)}
            hint={unavailableFeeds ? `${unavailableFeeds} 个异常` : "全部正常"}
          />
          <StatCard
            icon={Smile}
            label="表情库"
            value={formatNumber(data.stickers.total)}
            hint={`${formatNumber(data.stickers.banned)} 张禁用`}
          />
        </div>

        <UsagePanel
          data={usage}
          conversations={conversationOptions}
          days={usageDays}
          onDaysChange={onUsageDaysChange}
          conversation={conversation}
          onConversationChange={onConversationChange}
          loading={insightsLoading}
        />

        <FunPanel data={fun} loading={insightsLoading} />

        <div className="grid gap-6 xl:grid-cols-[1.45fr_1fr]">
          <SectionCard
            icon={Clock}
            title="定时任务"
            description="按群 / 私聊执行的定时消息"
            badge={`${data.tasks.enabled}/${data.tasks.total}`}
          >
            {data.task_rows.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ID</TableHead>
                    <TableHead>排期</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>下次</TableHead>
                    <TableHead>内容</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.task_rows.map((task) => (
                    <TableRow key={task.id}>
                      <TableCell className="font-mono text-xs text-slate-500">#{task.id}</TableCell>
                      <TableCell className="whitespace-nowrap">{scheduleText(task)}</TableCell>
                      <TableCell>
                        {task.enabled ? (
                          <Badge variant="success">
                            <CheckCircle2 className="mr-1 h-3 w-3" /> 运行中
                          </Badge>
                        ) : (
                          <Badge variant="muted">
                            <PauseCircle className="mr-1 h-3 w-3" /> 已暂停
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-slate-500">
                        {formatTs(task.next_run)}
                      </TableCell>
                      <TableCell className="max-w-[18rem]">
                        <span className="line-clamp-2" title={task.message}>
                          {task.message}
                        </span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <EmptyState icon={Clock} text="没有定时任务。" />
            )}
          </SectionCard>

          <SectionCard
            icon={Radio}
            title="RSS 订阅"
            description="B 站动态与通用 RSS/Atom"
            badge={String(data.feeds.total)}
          >
            {data.feed_rows.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>标题</TableHead>
                    <TableHead>目标</TableHead>
                    <TableHead>状态</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.feed_rows.map((feed) => (
                    <TableRow key={feed.id}>
                      <TableCell className="max-w-[16rem]">
                        <div className="font-medium text-slate-800">{feed.title}</div>
                        <div className="mt-1 truncate text-xs text-muted-foreground" title={feed.feed_url}>
                          {feed.feed_url}
                        </div>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-slate-500">
                        {feed.chat_type === "group" ? "群" : "私聊"} {feed.chat_id}
                      </TableCell>
                      <TableCell>
                        {feed.last_error ? (
                          <Badge variant="destructive" title={feed.last_error}>
                            <AlertTriangle className="mr-1 h-3 w-3" /> 异常
                          </Badge>
                        ) : (
                          <Badge variant="success">
                            <CheckCircle2 className="mr-1 h-3 w-3" /> 正常
                          </Badge>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <EmptyState icon={Rss} text="没有订阅。" />
            )}
          </SectionCard>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <SectionCard
            icon={Server}
            title="模型"
            description="SQLite 模型注册表"
            badge={String(data.model_rows.length)}
          >
            {data.model_rows.length ? (
              <div className="divide-y divide-blue-50">
                {data.model_rows.map((model) => (
                  <div
                    key={`${model.provider}/${model.model_id}`}
                    className="flex flex-wrap items-center justify-between gap-3 px-5 py-4"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-medium text-slate-800">
                          {model.provider}/{model.model_id}
                        </span>
                        {model.is_active ? <Badge>当前</Badge> : null}
                        {!model.enabled ? <Badge variant="muted">禁用</Badge> : null}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        上下文 {formatNumber(model.context_window)} tokens
                      </p>
                    </div>
                    <Hash className="h-4 w-4 text-blue-200" />
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState icon={Server} text="没有模型。" />
            )}
          </SectionCard>

          <SectionCard
            icon={CreditCard}
            title="角色卡"
            description="当前启用的人格与别名"
            badge={String(data.card_rows.length)}
          >
            {data.card_rows.length ? (
              <div className="divide-y divide-blue-50">
                {data.card_rows.map((card) => (
                  <div key={card.id} className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-slate-400">#{card.id}</span>
                        <span className="font-medium text-slate-800">{card.nickname || card.name}</span>
                        {card.is_active ? <Badge>当前</Badge> : null}
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {card.aliases.length ? (
                          card.aliases.map((alias) => (
                            <Badge key={alias} variant="secondary">
                              {alias}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-xs text-muted-foreground">无别名</span>
                        )}
                      </div>
                    </div>
                    <Activity className="h-4 w-4 text-blue-200" />
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState icon={CreditCard} text="没有角色卡。" />
            )}
          </SectionCard>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 pb-4 text-xs text-muted-foreground">
          <span>只读面板 · 数据直接读取 SQLite</span>
          <span>最后刷新 {formatTs(data.generated_at)}</span>
        </footer>
      </main>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState<"loading" | "login" | "dashboard">("loading");
  const [data, setData] = useState<Dashboard | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [loginError, setLoginError] = useState("");
  const [usageDays, setUsageDays] = useState(7);
  const [conversation, setConversation] = useState("");
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [fun, setFun] = useState<FunData | null>(null);
  const [insightsLoading, setInsightsLoading] = useState(false);

  const loadDashboard = useCallback(async () => {
    setRefreshing(true);
    try {
      const payload = await requestJson<Dashboard>("/api/dashboard");
      setData(payload);
      setError("");
      setLoginError("");
      setView("dashboard");
    } catch (err) {
      if (err instanceof Unauthorized) {
        setView("login");
        setData(null);
        return;
      }
      const message = err instanceof Error ? err.message : "加载失败";
      setError(message);
      setView((current) => (current === "loading" ? "login" : current));
      setLoginError((current) => current || message);
    } finally {
      setRefreshing(false);
    }
  }, []);

  const loadInsights = useCallback(async () => {
    setInsightsLoading(true);
    try {
      const params = new URLSearchParams({ days: String(usageDays) });
      if (conversation) params.set("conversation", conversation);
      const funQuery = conversation
        ? `?conversation=${encodeURIComponent(conversation)}`
        : "";
      const [nextUsage, nextFun] = await Promise.all([
        requestJson<UsageData>(`/api/usage?${params.toString()}`),
        requestJson<FunData>(`/api/fun${funQuery}`),
      ]);
      setUsage(nextUsage);
      setFun(nextFun);
    } catch (err) {
      if (err instanceof Unauthorized) {
        setView("login");
        setData(null);
      } else {
        setError(err instanceof Error ? err.message : "用量数据加载失败");
      }
    } finally {
      setInsightsLoading(false);
    }
  }, [usageDays, conversation]);

  const conversationOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of usage?.conversations ?? []) map.set(item.id, item.label);
    for (const item of fun?.conversations ?? []) {
      if (!map.has(item.id)) map.set(item.id, item.label);
    }
    if (conversation && !map.has(conversation)) {
      map.set(conversation, conversationLabel(conversation));
    }
    return [...map.entries()].map(([id, label]) => ({ id, label }));
  }, [usage, fun, conversation]);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const session = await requestJson<{ authenticated: boolean }>("/api/session");
        if (!active) return;
        if (session.authenticated) {
          await loadDashboard();
        } else {
          setView("login");
        }
      } catch (err) {
        if (!active) return;
        setView("login");
        setLoginError(err instanceof Error ? err.message : "无法连接管理服务");
      }
    })();
    return () => {
      active = false;
    };
  }, [loadDashboard]);

  useEffect(() => {
    if (view !== "dashboard") return;
    const timer = window.setInterval(() => {
      void loadDashboard();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [view, loadDashboard]);

  useEffect(() => {
    if (view !== "dashboard") return;
    void loadInsights();
  }, [view, loadInsights]);

  async function handleLogin(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setLoginError("");
    try {
      await requestJson<{ ok: boolean }>("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      setPassword("");
      await loadDashboard();
    } catch (err) {
      if (err instanceof Unauthorized) {
        setLoginError("密码不对。");
      } else {
        setLoginError(err instanceof Error ? err.message : "登录失败");
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleLogout() {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" }).catch(() => undefined);
    setData(null);
    setView("login");
  }

  if (view === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#f4f8ff] text-sm text-slate-500">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin text-blue-600" />
        正在加载…
      </div>
    );
  }

  if (view === "login" || !data) {
    return (
      <LoginScreen
        password={password}
        setPassword={setPassword}
        busy={busy}
        error={loginError}
        onSubmit={handleLogin}
      />
    );
  }

  return (
    <DashboardView
      data={data}
      refreshing={refreshing}
      error={error}
      onRefresh={() => void loadDashboard()}
      onLogout={() => void handleLogout()}
      usage={usage}
      fun={fun}
      insightsLoading={insightsLoading}
      usageDays={usageDays}
      conversation={conversation}
      conversationOptions={conversationOptions}
      onUsageDaysChange={setUsageDays}
      onConversationChange={setConversation}
    />
  );
}

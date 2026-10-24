import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { AlertTriangle, RefreshCw, ShieldCheck, Waves } from "lucide-react";

import AppShell from "@/components/layout/AppShell";
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
import { requestJson, setUnauthorizedHandler, Unauthorized } from "@/lib/api";
import { useHashRoute, type RouteId } from "@/lib/router";
import CardsPage from "@/pages/CardsPage";
import ConversationsPage from "@/pages/ConversationsPage";
import FeedsPage from "@/pages/FeedsPage";
import InsightsPage from "@/pages/InsightsPage";
import ModelsPage from "@/pages/ModelsPage";
import OverviewPage from "@/pages/OverviewPage";
import StickersPage from "@/pages/StickersPage";
import SystemPage from "@/pages/SystemPage";
import TasksPage from "@/pages/TasksPage";
import UsagePage from "@/pages/UsagePage";

type View = "loading" | "login" | "ready";

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
              仪表盘 · 会话 · 自动化 · 内容 · 用量
            </p>
            <p className="text-center text-[11px] text-slate-400">
              仅限本机 / 白名单访问 · 密码来自 bot.env
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Page({
  route,
  refreshKey,
  navigate,
}: {
  route: RouteId;
  refreshKey: number;
  navigate: (id: RouteId) => void;
}) {
  switch (route) {
    case "conversations":
      return <ConversationsPage refreshKey={refreshKey} />;
    case "insights":
      return <InsightsPage refreshKey={refreshKey} />;
    case "tasks":
      return <TasksPage refreshKey={refreshKey} />;
    case "feeds":
      return <FeedsPage refreshKey={refreshKey} />;
    case "models":
      return <ModelsPage refreshKey={refreshKey} />;
    case "cards":
      return <CardsPage refreshKey={refreshKey} />;
    case "stickers":
      return <StickersPage refreshKey={refreshKey} />;
    case "usage":
      return <UsagePage refreshKey={refreshKey} />;
    case "system":
      return <SystemPage refreshKey={refreshKey} />;
    default:
      return <OverviewPage refreshKey={refreshKey} onNavigate={navigate} />;
  }
}

export default function App() {
  const [view, setView] = useState<View>("loading");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const { route, navigate } = useHashRoute();

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setView("login");
      setPassword("");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let active = true;
    requestJson<{ authenticated: boolean }>("/api/session")
      .then((session) => {
        if (!active) return;
        setView(session.authenticated ? "ready" : "login");
      })
      .catch((err) => {
        if (!active) return;
        setView("login");
        setLoginError(err instanceof Error ? err.message : "无法连接管理服务");
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (view !== "ready") return;
    const timer = window.setInterval(() => setRefreshKey((value) => value + 1), 60_000);
    return () => window.clearInterval(timer);
  }, [view]);

  const refresh = useCallback(() => {
    setRefreshing(true);
    setRefreshKey((value) => value + 1);
    window.setTimeout(() => setRefreshing(false), 700);
  }, []);

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
      setRefreshKey((value) => value + 1);
      setView("ready");
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
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" }).catch(
      () => undefined
    );
    setView("login");
    setPassword("");
  }

  if (view === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#f4f8ff] text-sm text-slate-500">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin text-blue-600" />
        正在加载…
      </div>
    );
  }

  if (view === "login") {
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
    <AppShell
      route={route}
      onNavigate={navigate}
      onRefresh={refresh}
      onLogout={() => void handleLogout()}
      refreshing={refreshing}
    >
      <Page route={route} refreshKey={refreshKey} navigate={navigate} />
    </AppShell>
  );
}

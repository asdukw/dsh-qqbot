import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { RefreshCw, ShieldCheck, Waves } from "lucide-react";

import AppShell from "@/components/layout/AppShell";
import { Notice } from "@/components/common/Bits";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { requestJson, setUnauthorizedHandler, Unauthorized } from "@/lib/api";
import { useHashRoute, type RouteId } from "@/lib/router";
import CardsPage from "@/pages/CardsPage";
import FeedsPage from "@/pages/FeedsPage";
import InsightsPage from "@/pages/InsightsPage";
import ModelsPage from "@/pages/ModelsPage";
import OverviewPage from "@/pages/OverviewPage";
import RequestsPage from "@/pages/RequestsPage";
import StickersPage from "@/pages/StickersPage";
import StoryPage from "@/pages/StoryPage";
import SystemPage from "@/pages/SystemPage";
import TasksPage from "@/pages/TasksPage";
import UsagePage from "@/pages/UsagePage";
import VoicePage from "@/pages/VoicePage";

type View = "loading" | "login" | "ready";

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
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
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-canvas p-4">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_10%,rgba(59,130,246,0.10),transparent_38%),radial-gradient(circle_at_80%_90%,rgba(147,197,253,0.18),transparent_42%)]" />
      <Card className="relative w-full max-w-sm">
        <CardHeader className="items-center gap-1 pb-4 text-center">
          <span className="mb-2 flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-600 text-white shadow-lg shadow-blue-600/25">
            <Waves className="h-6 w-6" />
          </span>
          <CardTitle className="text-lg">大肥鱼 Admin</CardTitle>
          <CardDescription>QQ bot 运行面板</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form className="space-y-3" onSubmit={onSubmit}>
            <label className="block space-y-1.5 text-sm font-medium text-slate-700" htmlFor="password">
              管理员密码
              <Input
                id="password"
                type="password"
                autoFocus
                autoComplete="current-password"
                placeholder="ADMIN_UI_PASSWORD"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            {error ? <Notice tone="danger">{error}</Notice> : null}
            <Button className="w-full" type="submit" disabled={busy || !password}>
              {busy ? (
                <RefreshCw className="h-4 w-4 animate-spin" />
              ) : (
                <ShieldCheck className="h-4 w-4" />
              )}
              {busy ? "登录中…" : "登录"}
            </Button>
          </form>
          <p className="text-center text-[11px] text-slate-400">仅限本机或白名单地址访问</p>
        </CardContent>
      </Card>
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
    case "insights":
      return <InsightsPage refreshKey={refreshKey} />;
    case "requests":
      return <RequestsPage refreshKey={refreshKey} />;
    case "tasks":
      return <TasksPage refreshKey={refreshKey} />;
    case "feeds":
      return <FeedsPage refreshKey={refreshKey} />;
    case "models":
      return <ModelsPage refreshKey={refreshKey} />;
    case "story":
      return <StoryPage refreshKey={refreshKey} />;
    case "cards":
      return <CardsPage refreshKey={refreshKey} />;
    case "stickers":
      return <StickersPage refreshKey={refreshKey} />;
    case "usage":
      return <UsagePage refreshKey={refreshKey} />;
    case "voice":
      return <VoicePage refreshKey={refreshKey} />;
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
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
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
    setLastUpdated(nowSeconds());
    const timer = window.setInterval(() => {
      setRefreshKey((value) => value + 1);
      setLastUpdated(nowSeconds());
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [view]);

  const refresh = useCallback(() => {
    setRefreshing(true);
    setRefreshKey((value) => value + 1);
    setLastUpdated(nowSeconds());
    window.setTimeout(() => setRefreshing(false), 600);
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
      setLastUpdated(nowSeconds());
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
    setLastUpdated(null);
  }

  if (view === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas text-sm text-slate-500">
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
      lastUpdated={lastUpdated}
    >
      <Page route={route} refreshKey={refreshKey} navigate={navigate} />
    </AppShell>
  );
}

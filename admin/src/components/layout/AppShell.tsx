import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { LogOut, Menu, RefreshCw, Waves, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { NAV_GROUPS, routeMeta, type RouteId } from "@/lib/router";
import { formatClock } from "@/lib/format";
import { cn } from "@/lib/utils";

const ISLAND =
  "rounded-2xl border border-slate-200/70 bg-white/95 shadow-soft backdrop-blur-sm";

function Brand() {
  return (
    <span className="flex items-center gap-3">
      <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-600 text-white shadow-sm shadow-blue-600/25">
        <Waves className="h-4 w-4" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold tracking-tight text-slate-900">
          大肥鱼 Admin
        </span>
        <span className="block truncate text-xs text-slate-500">QQ bot 运行面板</span>
      </span>
    </span>
  );
}

function SidebarNav({
  route,
  onNavigate,
}: {
  route: RouteId;
  onNavigate: (id: RouteId) => void;
}) {
  return (
    <nav className="flex-1 space-y-5 overflow-y-auto px-2 py-3" aria-label="主导航">
      {NAV_GROUPS.map((group) => (
        <div key={group.label}>
          <p className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-slate-400">
            {group.label}
          </p>
          <div className="space-y-0.5">
            {group.items.map((item) => {
              const Icon = item.icon;
              const active = item.id === route;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => onNavigate(item.id)}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm font-medium transition-colors",
                    active
                      ? "bg-blue-50 text-blue-700"
                      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  )}
                >
                  <Icon
                    className={cn(
                      "h-4 w-4 shrink-0",
                      active ? "text-blue-600" : "text-slate-400"
                    )}
                  />
                  <span className="truncate">{item.label}</span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );
}

function SidebarFoot() {
  return (
    <div className="flex items-center gap-2 border-t border-slate-100 px-4 py-3 text-[11px] text-slate-400">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden="true" />
      每 60 秒自动刷新
    </div>
  );
}

export default function AppShell({
  route,
  onNavigate,
  onRefresh,
  onLogout,
  refreshing,
  lastUpdated,
  children,
}: {
  route: RouteId;
  onNavigate: (id: RouteId) => void;
  onRefresh: () => void;
  onLogout: () => void;
  refreshing: boolean;
  lastUpdated: number | null;
  children: ReactNode;
}) {
  const meta = routeMeta(route);
  const [menuOpen, setMenuOpen] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  return (
    <div className="min-h-screen bg-canvas px-3 py-3 md:px-4 md:py-4 lg:flex lg:items-start lg:gap-4">
      <a
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main")?.focus();
        }}
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow-lg"
      >
        跳到主要内容
      </a>

      <aside
        className={cn(
          ISLAND,
          "sticky top-4 hidden h-[calc(100vh-2rem)] w-60 shrink-0 flex-col lg:flex"
        )}
      >
        <div className="px-3 py-3">
          <button
            type="button"
            onClick={() => onNavigate("overview")}
            className="rounded-lg px-1 py-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Brand />
          </button>
        </div>
        <SidebarNav route={route} onNavigate={onNavigate} />
        <SidebarFoot />
      </aside>

      {menuOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/30 backdrop-blur-sm"
            onClick={() => setMenuOpen(false)}
            aria-label="关闭导航"
          />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label="主导航"
            className="absolute inset-y-3 left-3 flex w-72 max-w-[85%] flex-col rounded-2xl border border-slate-200 bg-white shadow-2xl"
          >
            <div className="flex items-center justify-between px-3 py-3">
              <Brand />
              <Button
                ref={closeRef}
                variant="ghost"
                size="icon"
                onClick={() => setMenuOpen(false)}
                aria-label="关闭导航"
              >
                <X className="h-5 w-5" />
              </Button>
            </div>
            <SidebarNav
              route={route}
              onNavigate={(id) => {
                onNavigate(id);
                setMenuOpen(false);
              }}
            />
            <SidebarFoot />
          </aside>
        </div>
      ) : null}

      <div className="min-w-0 flex-1 space-y-4">
        <header className={cn(ISLAND, "sticky top-3 z-30 md:top-4")}>
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 md:px-5">
            <div className="flex min-w-0 items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="lg:hidden"
                onClick={() => setMenuOpen(true)}
                aria-label="打开导航"
              >
                <Menu className="h-5 w-5" />
              </Button>
              <div className="min-w-0">
                <h1 className="truncate text-base font-semibold tracking-tight text-slate-900">
                  {meta.label}
                </h1>
                <p className="truncate text-xs text-slate-500">{meta.description}</p>
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              {lastUpdated ? (
                <span className="hidden text-xs text-slate-400 sm:inline">
                  更新于 {formatClock(lastUpdated)}
                </span>
              ) : null}
              <Button
                variant="outline"
                size="sm"
                onClick={onRefresh}
                disabled={refreshing}
                aria-label="刷新数据"
              >
                <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
                <span className="hidden sm:inline">刷新</span>
              </Button>
              <Button variant="ghost" size="sm" onClick={onLogout} aria-label="退出登录">
                <LogOut className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">退出</span>
              </Button>
            </div>
          </div>
        </header>

        <main id="main" tabIndex={-1} className="pb-4 focus:outline-none">
          {children}
        </main>
      </div>
    </div>
  );
}

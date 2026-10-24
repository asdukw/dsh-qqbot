import { useState } from "react";
import type { ReactNode } from "react";
import { LogOut, Menu, RefreshCw, Waves, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NAV_GROUPS, routeMeta, type RouteId } from "@/lib/router";
import { cn } from "@/lib/utils";

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <div
        className={cn(
          "flex items-center justify-center rounded-xl bg-blue-600 text-white shadow-md shadow-blue-600/20",
          compact ? "h-9 w-9" : "h-10 w-10"
        )}
      >
        <Waves className={compact ? "h-4 w-4" : "h-5 w-5"} />
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-base font-semibold tracking-tight text-slate-900">
            大肥鱼 Admin
          </span>
          <Badge variant="secondary">只读</Badge>
        </div>
        <p className="truncate text-xs text-muted-foreground">QQ bot 运行面板</p>
      </div>
    </div>
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
    <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4" aria-label="主导航">
      {NAV_GROUPS.map((group) => (
        <div key={group.label}>
          <p className="px-3 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-400">
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
                    "flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    active
                      ? "bg-blue-600 text-white shadow-sm"
                      : "text-slate-600 hover:bg-blue-50 hover:text-blue-700"
                  )}
                >
                  <Icon
                    className={cn(
                      "h-4 w-4 shrink-0",
                      active ? "text-white" : "text-slate-400"
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
    <div className="border-t border-blue-50 px-5 py-4 text-[11px] leading-relaxed text-slate-400">
      只读面板 · 数据直接读取 SQLite
      <br />
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
  children,
}: {
  route: RouteId;
  onNavigate: (id: RouteId) => void;
  onRefresh: () => void;
  onLogout: () => void;
  refreshing: boolean;
  children: ReactNode;
}) {
  const meta = routeMeta(route);
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <div className="min-h-screen bg-[#f4f8ff]">
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-64 flex-col border-r border-blue-100 bg-white lg:flex">
        <div className="border-b border-blue-50 px-4 py-4">
          <button type="button" onClick={() => onNavigate("overview")} className="text-left">
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
          <aside className="absolute inset-y-0 left-0 flex w-72 max-w-[85%] flex-col bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-blue-50 px-4 py-3">
              <Brand compact />
              <Button
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

      <div className="lg:pl-64">
        <header className="sticky top-0 z-30 border-b border-blue-100 bg-white/85 backdrop-blur">
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 md:px-8">
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
                <div className="flex items-center gap-2">
                  <h1 className="truncate text-lg font-semibold tracking-tight text-slate-900">
                    {meta.label}
                  </h1>
                  <Badge variant="secondary" className="hidden sm:inline-flex">
                    只读
                  </Badge>
                </div>
                <p className="truncate text-xs text-muted-foreground">{meta.description}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
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
          {children}
        </main>
      </div>
    </div>
  );
}

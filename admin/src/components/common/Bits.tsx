import type { ComponentType, ReactNode } from "react";
import { AlertTriangle, Loader2, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

type Icon = ComponentType<{ className?: string }>;

const TONES = {
  blue: "bg-blue-50 text-blue-600",
  emerald: "bg-emerald-50 text-emerald-600",
  amber: "bg-amber-50 text-amber-600",
  red: "bg-red-50 text-red-600",
  slate: "bg-slate-100 text-slate-600",
} as const;

export type Tone = keyof typeof TONES;

export function StatCard({
  icon: Icon,
  label,
  value,
  hint,
  tone = "blue",
  onClick,
}: {
  icon: Icon;
  label: string;
  value: string;
  hint?: string;
  tone?: Tone;
  onClick?: () => void;
}) {
  const body = (
    <CardContent className="flex items-start justify-between p-5">
      <div className="min-w-0">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
        <p className="mt-2 truncate text-xl font-semibold text-slate-900" title={value}>
          {value}
        </p>
        {hint ? <p className="mt-1 truncate text-xs text-muted-foreground">{hint}</p> : null}
      </div>
      <div className={cn("ml-3 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl", TONES[tone])}>
        <Icon className="h-5 w-5" />
      </div>
    </CardContent>
  );

  if (onClick) {
    return (
      <button type="button" onClick={onClick} className="w-full text-left">
        <Card className="h-full transition-shadow hover:shadow-md">{body}</Card>
      </button>
    );
  }
  return <Card className="h-full transition-shadow hover:shadow-md">{body}</Card>;
}

export function SectionCard({
  icon: Icon,
  title,
  description,
  badge,
  actions,
  children,
  contentClassName,
}: {
  icon?: Icon;
  title: string;
  description?: string;
  badge?: string | number;
  actions?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
}) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-3 space-y-0 border-b border-blue-50 pb-4">
        <div className="flex items-start gap-3">
          {Icon ? (
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
              <Icon className="h-4 w-4" />
            </div>
          ) : null}
          <div>
            <CardTitle>{title}</CardTitle>
            {description ? <CardDescription className="mt-1">{description}</CardDescription> : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {badge !== undefined ? <Badge variant="outline">{badge}</Badge> : null}
          {actions}
        </div>
      </CardHeader>
      <CardContent className={cn("p-0", contentClassName)}>{children}</CardContent>
    </Card>
  );
}

export function EmptyState({ icon: Icon, text, hint }: { icon: Icon; text: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center text-sm text-muted-foreground">
      <Icon className="h-6 w-6 text-blue-200" />
      <p className="font-medium text-slate-600">{text}</p>
      {hint ? <p className="max-w-sm text-xs">{hint}</p> : null}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

export function LoadingHint({ loading, label = "加载中…" }: { loading: boolean; label?: string }) {
  if (!loading) return null;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
      <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-600" />
      {label}
    </span>
  );
}

export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      {children}
    </div>
  );
}

export function PillTabs<T extends string | number>({
  value,
  options,
  onChange,
  className,
}: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div className={cn("inline-flex rounded-lg border border-blue-200 bg-white p-0.5", className)}>
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            value === option.value
              ? "bg-blue-600 text-white"
              : "text-slate-500 hover:bg-blue-50 hover:text-blue-700"
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function SearchField({
  value,
  onChange,
  placeholder = "搜索…",
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  return (
    <div className={cn("relative", className)}>
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
      <Input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="h-9 pl-9"
      />
    </div>
  );
}

export function InfoRow({ label, value, mono }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 px-5 py-3">
      <span className="text-xs text-slate-500">{label}</span>
      <span className={cn("max-w-[70%] break-all text-right text-sm text-slate-800", mono && "font-mono text-xs")}>
        {value}
      </span>
    </div>
  );
}

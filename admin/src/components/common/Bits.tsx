import type { ComponentType, ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  Search,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

type Icon = ComponentType<{ className?: string }>;

const ICON_TONES = {
  blue: "bg-blue-50 text-blue-600",
  emerald: "bg-emerald-50 text-emerald-600",
  amber: "bg-amber-50 text-amber-600",
  red: "bg-red-50 text-red-600",
  slate: "bg-slate-100 text-slate-600",
} as const;

export type Tone = keyof typeof ICON_TONES;

/** Vertical rhythm shared by every page. `hasData` avoids dimming skeletons. */
export function PageStack({
  error,
  loading = false,
  hasData = true,
  children,
  className,
}: {
  error?: string;
  loading?: boolean;
  hasData?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      aria-busy={loading || undefined}
      className={cn(
        "space-y-5",
        loading && hasData && "opacity-60 transition-opacity",
        className
      )}
    >
      {error ? <ErrorBanner message={error} /> : null}
      {children}
    </div>
  );
}

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
    <CardContent className="flex items-start justify-between gap-3 p-4">
      <div className="min-w-0 space-y-1">
        <p className="text-xs font-medium text-slate-500">{label}</p>
        <p
          className="truncate text-2xl font-semibold tracking-tight text-slate-900 tabular-nums"
          title={value}
        >
          {value}
        </p>
        {hint ? <p className="truncate text-xs text-slate-500">{hint}</p> : null}
      </div>
      <span
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
          ICON_TONES[tone]
        )}
      >
        <Icon className="h-4 w-4" />
      </span>
    </CardContent>
  );

  const cardClass = "h-full transition-colors";
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="group w-full rounded-2xl text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <Card className={cn(cardClass, "group-hover:border-slate-300")}>{body}</Card>
      </button>
    );
  }
  return <Card className={cardClass}>{body}</Card>;
}

export function SectionCard({
  icon: Icon,
  title,
  description,
  badge,
  actions,
  children,
  contentClassName,
  className,
}: {
  icon?: Icon;
  title: string;
  description?: string;
  badge?: string | number;
  actions?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
  className?: string;
}) {
  return (
    <Card className={cn("overflow-hidden", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-5 py-4">
        <div className="flex min-w-0 items-start gap-3">
          {Icon ? (
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-500">
              <Icon className="h-4 w-4" />
            </span>
          ) : null}
          <div className="min-w-0">
            <CardTitle>{title}</CardTitle>
            {description ? (
              <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                {description}
              </p>
            ) : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {badge !== undefined ? <Badge variant="outline">{badge}</Badge> : null}
          {actions}
        </div>
      </div>
      <div className={contentClassName}>{children}</div>
    </Card>
  );
}

export function SectionHeading({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-2">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {description ? (
          <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  text,
  hint,
}: {
  icon: Icon;
  text: string;
  hint?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <span className="flex h-10 w-10 items-center justify-center rounded-full bg-slate-100">
        <Icon className="h-5 w-5 text-slate-400" />
      </span>
      <p className="text-sm font-medium text-slate-600">{text}</p>
      {hint ? <p className="max-w-sm text-xs leading-relaxed text-slate-500">{hint}</p> : null}
    </div>
  );
}

const NOTICE_TONES = {
  info: { box: "border-blue-200 bg-blue-50 text-blue-800", icon: Info },
  success: { box: "border-emerald-200 bg-emerald-50 text-emerald-800", icon: CheckCircle2 },
  warning: { box: "border-amber-200 bg-amber-50 text-amber-800", icon: AlertTriangle },
  danger: { box: "border-red-200 bg-red-50 text-red-800", icon: AlertTriangle },
} as const;

export function Notice({
  tone = "info",
  children,
  className,
}: {
  tone?: keyof typeof NOTICE_TONES;
  children: ReactNode;
  className?: string;
}) {
  const { box, icon: Icon } = NOTICE_TONES[tone];
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={cn(
        "flex items-start gap-2 rounded-xl border px-4 py-3 text-sm leading-relaxed",
        box,
        className
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return <Notice tone="danger">{message}</Notice>;
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

/** Filter row above a table: filters on the left, secondary actions on the right. */
export function FilterBar({
  children,
  actions,
}: {
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">{children}</div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function PillTabs<T extends string | number>({
  value,
  options,
  onChange,
  className,
  label,
}: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
  className?: string;
  label?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("inline-flex rounded-lg border border-slate-200 bg-white p-0.5", className)}
    >
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={cn(
            "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            value === option.value
              ? "bg-primary text-white shadow-sm"
              : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
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
        aria-label={placeholder}
        className="pl-9"
      />
    </div>
  );
}

export function InfoList({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("divide-y divide-slate-100", className)}>{children}</div>;
}

export function InfoRow({ label, value, mono }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-2.5">
      <span className="text-xs text-slate-500">{label}</span>
      <span
        className={cn(
          "max-w-[70%] break-all text-right text-sm text-slate-800",
          mono && "font-mono text-xs"
        )}
      >
        {value}
      </span>
    </div>
  );
}

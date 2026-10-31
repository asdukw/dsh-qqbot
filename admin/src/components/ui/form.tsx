import type { ReactNode } from "react";

import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";

/** Select whose onChange hands back the value directly. */
export function FormSelect({
  value,
  onChange,
  disabled,
  id,
  children,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  id?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Select
      id={id}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      className={className}
    >
      {children}
    </Select>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
  className,
}: {
  label: ReactNode;
  hint?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("block space-y-1.5", className)} htmlFor={htmlFor}>
      <span className="block text-xs font-medium text-slate-600">{label}</span>
      {children}
      {hint ? (
        <span className="block text-[11px] leading-relaxed text-slate-400">{hint}</span>
      ) : null}
    </label>
  );
}

export function CheckboxRow({
  label,
  hint,
  checked,
  onChange,
  disabled,
  className,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={cn(
        "flex items-start gap-2.5 rounded-lg border border-slate-200 px-3 py-2.5",
        disabled && "opacity-60",
        className
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 h-4 w-4 rounded border-slate-300 accent-blue-600"
      />
      <span className="min-w-0">
        <span className="block text-sm text-slate-700">{label}</span>
        {hint ? (
          <span className="mt-0.5 block text-[11px] leading-relaxed text-slate-400">{hint}</span>
        ) : null}
      </span>
    </label>
  );
}

import clsx from "clsx";
import React from "react";

/* ----------------------------- Button ----------------------------------- */
type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "outline" | "coral" | "danger";
  size?: "sm" | "md";
};

export function Button({
  variant = "outline",
  size = "md",
  className,
  ...rest
}: ButtonProps) {
  return (
    <button
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-teal/50 disabled:opacity-40 disabled:cursor-not-allowed",
        size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-3.5 py-2 text-sm",
        variant === "primary" &&
          "bg-teal text-ink hover:bg-teal/90 font-semibold",
        variant === "coral" &&
          "bg-coral text-ink hover:bg-coral/90 font-semibold",
        variant === "outline" &&
          "border border-line bg-panel-2 text-text hover:border-teal/60 hover:text-white",
        variant === "ghost" && "text-muted hover:text-text hover:bg-panel-2",
        variant === "danger" &&
          "border border-fail/40 bg-transparent text-fail hover:bg-fail/10",
        className
      )}
      {...rest}
    />
  );
}

/* ------------------------------ Card ------------------------------------- */
export function Card({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={clsx(
        "rounded-xl border border-line bg-panel shadow-panel",
        className
      )}
    >
      {children}
    </div>
  );
}

/* ------------------------------ Field ------------------------------------ */
export function Label({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-muted">
      {children}
    </label>
  );
}

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...rest }, ref) => (
  <input
    ref={ref}
    className={clsx(
      "w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm text-text",
      "placeholder:text-muted/60 focus:border-teal/60 focus:outline-none",
      className
    )}
    {...rest}
  />
));
Input.displayName = "Input";

export function Textarea({
  className,
  ...rest
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={clsx(
        "w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm text-text",
        "placeholder:text-muted/60 focus:border-teal/60 focus:outline-none",
        className
      )}
      {...rest}
    />
  );
}

export function Select({
  className,
  children,
  ...rest
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={clsx(
        "w-full appearance-none rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm text-text",
        "focus:border-teal/60 focus:outline-none",
        className
      )}
      {...rest}
    >
      {children}
    </select>
  );
}

/* --------------------------- Section title ------------------------------- */
export function SectionTitle({
  children,
  sub,
}: {
  children: React.ReactNode;
  sub?: string;
}) {
  return (
    <div className="mb-3">
      <h2 className="font-display text-lg font-semibold text-text">{children}</h2>
      {sub && <p className="mt-0.5 text-sm text-muted">{sub}</p>}
    </div>
  );
}

/* ------------------------------- Toast ----------------------------------- */
export function Banner({
  tone = "info",
  children,
}: {
  tone?: "info" | "good" | "warn" | "fail";
  children: React.ReactNode;
}) {
  return (
    <div
      className={clsx(
        "rounded-lg border px-4 py-3 text-sm",
        tone === "info" && "border-teal/30 bg-teal/5 text-teal",
        tone === "good" && "border-good/30 bg-good/5 text-good",
        tone === "warn" && "border-warn/30 bg-warn/5 text-warn",
        tone === "fail" && "border-fail/30 bg-fail/5 text-fail"
      )}
    >
      {children}
    </div>
  );
}

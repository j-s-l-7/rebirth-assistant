"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useInvestorId } from "@/lib/investor";

export function Card({ title, action, children, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl border border-line bg-surface p-5 ${className}`}>
      {(title || action) && (
        <div className="flex items-start justify-between gap-3 mb-4">
          {title ? <h2 className="text-sm font-semibold tracking-wide text-muted uppercase">{title}</h2> : <span />}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

const BADGE = {
  neutral: "bg-surface-2 text-muted",
  good: "bg-good-soft text-good",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
  info: "bg-info-soft text-info",
  brand: "bg-brand-soft text-ink",
};

export function Badge({ tone = "neutral", children }: { tone?: keyof typeof BADGE; children: ReactNode }) {
  return <span className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${BADGE[tone]}`}>{children}</span>;
}

export function Button({
  children,
  onClick,
  variant = "primary",
  disabled,
  type = "button",
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
}) {
  const styles =
    variant === "primary"
      ? "bg-brand text-brand-ink hover:opacity-90"
      : "border border-line text-ink hover:bg-surface-2";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-2 rounded-full px-4 py-2 text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted py-8 justify-center" role="status">
      <span className="ra-dot h-2 w-2 rounded-full bg-accent" />
      {label}…
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return <div className="rounded-xl border border-bad/30 bg-bad-soft text-bad text-sm px-4 py-3">{message}</div>;
}

/** Renders children only once an investor is selected; otherwise points to onboarding. */
export function RequireInvestor({ children }: { children: (investorId: string) => ReactNode }) {
  const [id] = useInvestorId();
  if (id === undefined) return <Spinner />;
  if (!id)
    return (
      <div className="max-w-md mx-auto text-center py-16 space-y-4">
        <p className="font-serif text-2xl">Let&apos;s start with your funds</p>
        <p className="text-muted">Import your CAS statement or explore RA with a demo portfolio.</p>
        <Link href="/" className="inline-flex rounded-full bg-brand text-brand-ink px-4 py-2 text-sm font-medium">
          Get started
        </Link>
      </div>
    );
  return <>{children(id)}</>;
}

/** Minimal client-side fetch hook. Loading is derived from a request key, so state is only
 *  set from async callbacks; stale data stays visible while a reload is in flight. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[]) {
  const [tick, setTick] = useState(0);
  const key = JSON.stringify([...deps, tick]);
  const [state, setState] = useState<{ key?: string; data?: T; error?: string }>({});
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let live = true;
    loader()
      .then((data) => live && setState({ key, data }))
      .catch((e: Error) => live && setState({ key, error: e.message }));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const current = state.key === key;
  return { data: state.data, error: current ? state.error : undefined, loading: !current, reload };
}

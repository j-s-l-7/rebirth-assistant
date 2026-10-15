const whole = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

export function rupees(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n < 0 ? "−" : ""}₹${whole.format(Math.abs(Math.round(n)))}`;
}

/** ₹12.46 L / ₹1.20 Cr for large values; full rupees below a lakh. */
export function compact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "−" : "";
  const a = Math.abs(n);
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} L`;
  return `${sign}₹${whole.format(Math.round(a))}`;
}

export function pct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

export function signedPct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(digits)}%`;
}

export function dateIN(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export function tone(n: number | null | undefined): string {
  if (n == null) return "text-muted";
  return n > 0 ? "text-good" : n < 0 ? "text-bad" : "text-muted";
}

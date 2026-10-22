"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { Logo } from "@/components/Logo";
import { api } from "@/lib/api";

type Fund = { name: string; amfi_code: number; category: string };
type Exposure = { name: string; pct: number; funds: string[] };
type Check = { check: string; status: string; item: string; actual_pct: number | null; limit_pct: number | null };
type ApiError = { fund: string; code: string; message: string; candidates?: string[] };
type Missing = { fund: string; need: string };
type Xray = {
  disclosure_months: Record<string, string>;
  coverage: { covered_value_pct: number; funds_without_disclosure: string[]; stale_disclosures: unknown[] };
  exposures: { securities: Exposure[]; sectors: Exposure[]; industries: Exposure[] };
  overlap: { fund_a: string; fund_b: string; overlap_pct: number; common_securities: number }[];
  concentration: { top10_pct: number; effective_stocks: number; top_sector_pct: number };
  objective_checks: Check[];
  observations: { code: string; text: string }[];
  missing_details: Missing[];
  errors: ApiError[];
  assumptions: string[];
};
type Row = { id: number; fund: string; amount: string };

const MAX_ROWS = 20;
const input =
  "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-brand";
const btn =
  "rounded-full bg-brand px-5 py-2 text-sm text-brand-ink transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50";
const linkBtn = "text-sm text-muted hover:text-ink focus-visible:outline-2 focus-visible:outline-brand";

const pct = (n: number | null | undefined, d = 1) => (n == null ? "-" : `${n.toFixed(d)}%`);

function FundInput({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  const [options, setOptions] = useState<Fund[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const q = value.trim();
    if (!open || q.length < 2) return;
    let stale = false;
    const t = setTimeout(() => {
      api<Fund[]>(`/funds/search?q=${encodeURIComponent(q)}`)
        .then((r) => !stale && setOptions(r.slice(0, 10)))
        .catch(() => !stale && setOptions([]));
    }, 250);
    return () => {
      stale = true;
      clearTimeout(t);
    };
  }, [value, open]);

  return (
    <div className="relative">
      <input
        aria-label={label}
        className={input}
        value={value}
        placeholder="Fund name"
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      />
      {open && options.length > 0 && value.trim().length >= 2 ? (
        <ul className="absolute z-10 mt-1 max-h-72 w-full overflow-auto rounded-lg border border-line bg-surface shadow-sm">
          {options.map((o) => (
            <li key={o.amfi_code}>
              <button
                type="button"
                className="block w-full px-3 py-2 text-left text-sm hover:bg-surface-2"
                onMouseDown={(e) => {
                  e.preventDefault();
                  onChange(o.name);
                  setOpen(false);
                }}
              >
                {o.name} <span className="text-muted">({o.category})</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="mb-2 font-serif text-xl">{title}</h2>
      {children}
    </section>
  );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-line text-muted">
            {head.map((h) => (
              <th key={h} className="py-1.5 pr-4 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-line/60 align-top">
              {r.map((c, j) => (
                <td key={j} className="py-1.5 pr-4">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const Bar = ({ value }: { value: number }) => (
  <div className="flex items-center gap-2">
    <div className="h-2 w-24 rounded bg-surface-2">
      <div className="h-2 rounded bg-accent" style={{ width: `${Math.min(100, value)}%` }} />
    </div>
    <span>{pct(value, 2)}</span>
  </div>
);

function exposureTable(items: Exposure[], first: string, limit?: number) {
  return (
    <Table
      head={[first, "% of portfolio", "Funds"]}
      rows={items.slice(0, limit).map((e) => [e.name, <Bar key="b" value={e.pct} />, e.funds.join(", ")])}
    />
  );
}

function checkText(c: Check) {
  const a = pct(c.actual_pct);
  const l = pct(c.limit_pct);
  if (c.status === "exceeds") return `${c.item} ${a} - above your stated limit of ${l}`;
  if (c.status === "within") return `${c.item} ${a} - within your stated limit of ${l}`;
  return `${c.item}${c.actual_pct != null ? ` ${a}` : ""} - present in your portfolio`;
}

function Problems({ errors, missing }: { errors: ApiError[]; missing: Missing[] }) {
  if (!errors.length && !missing.length) return null;
  return (
    <Section title="Not included">
      <ul className="list-disc space-y-1 pl-5 text-sm">
        {errors.map((e, i) => (
          <li key={i}>
            {e.fund}: {e.message}
            {e.candidates?.length ? ` Matches: ${e.candidates.join("; ")}.` : ""}
          </li>
        ))}
        {missing.map((m, i) => (
          <li key={i}>
            {m.fund}: needs {m.need}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function Results({ r }: { r: Xray }) {
  const months = Object.entries(r.disclosure_months);
  return (
    <div>
      <Section title="Coverage">
        <p className="text-sm">
          {"Based on each fund's latest monthly disclosure; months shown. "}
          Disclosures cover {pct(r.coverage.covered_value_pct)} of the portfolio value.
          {r.coverage.funds_without_disclosure.length
            ? ` No disclosure loaded for: ${r.coverage.funds_without_disclosure.join(", ")}.`
            : ""}
        </p>
        <Table head={["Fund", "Disclosure month"]} rows={months.map(([f, m]) => [f, m])} />
      </Section>
      <Problems errors={r.errors} missing={r.missing_details} />
      <Section title="Top 10 securities">{exposureTable(r.exposures.securities, "Security", 10)}</Section>
      <Section title="Sectors">{exposureTable(r.exposures.sectors, "Sector")}</Section>
      <Section title="Industries">{exposureTable(r.exposures.industries, "Industry")}</Section>
      <Section title="Overlap between funds">
        {r.overlap.length ? (
          <Table
            head={["Fund A", "Fund B", "Overlap", "Common securities"]}
            rows={r.overlap.map((o) => [o.fund_a, o.fund_b, pct(o.overlap_pct), o.common_securities])}
          />
        ) : (
          <p className="text-sm text-muted">Fewer than two funds with disclosures.</p>
        )}
      </Section>
      <Section title="Concentration">
        <Table
          head={["Top 10 securities", "Effective number of stocks", "Top sector"]}
          rows={[[pct(r.concentration.top10_pct), r.concentration.effective_stocks.toFixed(1), pct(r.concentration.top_sector_pct)]]}
        />
      </Section>
      {r.objective_checks.length ? (
        <Section title="Your limits">
          <ul className="space-y-1.5 text-sm">
            {r.objective_checks.map((c, i) => (
              <li key={i} className="flex items-start gap-2">
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${c.status === "exceeds" ? "bg-bad/10 text-bad" : "bg-surface-2 text-muted"}`}
                >
                  {c.status}
                </span>
                <span>{checkText(c)}</span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      {r.observations.length ? (
        <Section title="Observations">
          <ul className="list-disc space-y-1.5 pl-5 text-sm">
            {r.observations.map((o, i) => (
              <li key={i}>{o.text}</li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

/** Portfolio X-ray: look-through exposure, overlap and concentration from AMC monthly disclosures. */
export default function XrayPage() {
  const [rows, setRows] = useState<Row[]>([{ id: 1, fund: "", amount: "" }]);
  const [nextId, setNextId] = useState(2);
  const [limits, setLimits] = useState({ sec: "", sector: "", industry: "", avoid: "" });
  const [result, setResult] = useState<Xray | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const update = (id: number, p: Partial<Row>) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...p } : r)));
  const num = (s: string) => (s.trim() && Number(s) > 0 ? Number(s) : undefined);
  const filled = rows.filter((r) => r.fund.trim());
  const holdings = filled.map((r) => ({ fund: r.fund.trim(), amount_inr: num(r.amount) }));

  async function run() {
    setBusy(true);
    setError("");
    const avoid = limits.avoid.split(",").map((s) => s.trim()).filter(Boolean);
    const objective = {
      max_security_pct: num(limits.sec),
      max_sector_pct: num(limits.sector),
      max_industry_pct: num(limits.industry),
      avoid,
    };
    try {
      setResult(await api<Xray>("/portfolio/xray", { method: "POST", body: JSON.stringify({ holdings, objective }) }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-16">
      <header className="flex h-14 items-center justify-between">
        <div className="flex items-center gap-2">
          <Logo size={24} />
          <span className="font-serif text-lg">RA</span>
        </div>
        <Link href="/chat" className={linkBtn}>
          Chat
        </Link>
      </header>
      <main>
        <h1 className="mt-4 font-serif text-3xl">Portfolio X-ray</h1>
        <p className="mt-1 text-sm text-muted">
          Enter the funds you hold and the amount in each, in rupees. The X-ray looks through to the securities they own.
        </p>

        <Section title="Your funds">
          <div className="space-y-2">
            {rows.map((r, i) => (
              <div key={r.id} className="flex gap-2">
                <div className="flex-1">
                  <FundInput label={`Fund ${i + 1}`} value={r.fund} onChange={(v) => update(r.id, { fund: v })} />
                </div>
                <input
                  aria-label={`Amount in rupees for fund ${i + 1}`}
                  className={`${input} !w-36`}
                  inputMode="decimal"
                  placeholder="Amount (INR)"
                  value={r.amount}
                  onChange={(e) => update(r.id, { amount: e.target.value })}
                />
                <button
                  type="button"
                  className={linkBtn}
                  disabled={rows.length === 1}
                  onClick={() => setRows((rs) => rs.filter((x) => x.id !== r.id))}
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
          <button
            type="button"
            className={`${linkBtn} mt-2`}
            disabled={rows.length >= MAX_ROWS}
            onClick={() => {
              setRows((rs) => [...rs, { id: nextId, fund: "", amount: "" }]);
              setNextId(nextId + 1);
            }}
          >
            + Add fund
          </button>
        </Section>

        <Section title="My own limits (optional)">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {(
              [
                ["sec", "Max single security %"],
                ["sector", "Max sector %"],
                ["industry", "Max industry %"],
              ] as const
            ).map(([k, label]) => (
              <label key={k} className="text-sm text-muted">
                {label}
                <input
                  className={`${input} mt-1`}
                  inputMode="decimal"
                  value={limits[k]}
                  onChange={(e) => setLimits({ ...limits, [k]: e.target.value })}
                />
              </label>
            ))}
          </div>
          <label className="mt-2 block text-sm text-muted">
            Avoid (comma separated names)
            <input
              className={`${input} mt-1`}
              value={limits.avoid}
              onChange={(e) => setLimits({ ...limits, avoid: e.target.value })}
            />
          </label>
        </Section>

        <div className="mt-6">
          <button type="button" className={btn} disabled={busy || filled.length === 0} onClick={run}>
            {busy ? "Working..." : "Show my portfolio X-ray"}
          </button>
          {error ? <p className="mt-2 text-sm text-bad">{error}</p> : null}
        </div>

        {result ? <Results r={result} /> : null}
      </main>
      <footer className="mt-12 border-t border-line pt-4 text-xs text-muted">
        Information from AMC monthly portfolio disclosures. This is not a recommendation to act. A human expert (EUIN
        holder) can talk it through with you.
      </footer>
    </div>
  );
}

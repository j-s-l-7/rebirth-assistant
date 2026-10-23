"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Logo } from "@/components/Logo";
import { API_BASE, api } from "@/lib/api";

type Dataset = { key: string; label: string; have: number; total: number; pct: number };
type Coverage = {
  headline_pct: number;
  universe: number;
  datasets: Dataset[];
  reference_documents: { loaded: number; note: string };
};
type MissingRes = {
  total_missing: number;
  by_amc: { amc: string; missing: number; reason: string }[];
  funds: { amfi_code: number; name: string; amc: string; category: string }[];
};
type Item = {
  id: string;
  kind: "dataset" | "file";
  title: string;
  fund: { name: string; amfi_code: number } | null;
  amc: string;
  dataset_key: string;
  available: boolean;
  location: string;
  details: Record<string, unknown>;
  open_url: string;
};
type Cell = string | number | boolean | null;
type TableData = { columns: string[]; rows: Cell[][]; total: number; as_of: string | null; source: string | null };
type Preview = { sheets?: string[]; sheet?: string; rows?: string[][]; members?: { name: string; size: number }[] };
type FundHit = { name: string; amfi_code: number };

const LIMIT = 24;
const PAGE_ROWS = 200;
const input =
  "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-brand";
const btn =
  "rounded-full bg-brand px-5 py-2 text-sm text-brand-ink transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50";
const linkBtn =
  "text-sm text-muted hover:text-ink focus-visible:outline-2 focus-visible:outline-brand disabled:opacity-40";
const err = (e: unknown) => (e instanceof Error ? e.message : "Request failed");
const apiPath = (u: string) => u.replace(/^\/api/, "");
const fmt = (v: unknown) => (v == null || v === "" ? "-" : String(v));

function Grid({ head, rows }: { head: string[]; rows: Cell[][] }) {
  return (
    <div className="max-h-[28rem] overflow-auto">
      <table className="w-full text-left text-sm">
        <thead className="sticky top-0 bg-surface">
          <tr className="border-b border-line text-muted">
            {head.map((h, i) => (
              <th key={i} className="py-1.5 pr-4 font-normal whitespace-nowrap">
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
                  {fmt(c)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Pager({ offset, step, total, onChange }: { offset: number; step: number; total: number; onChange: (o: number) => void }) {
  if (total <= step) return null;
  return (
    <div className="mt-3 flex items-center gap-4 text-sm text-muted">
      <button type="button" className={linkBtn} disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - step))}>
        Prev
      </button>
      <span>
        {offset + 1}-{Math.min(offset + step, total)} of {total}
      </span>
      <button type="button" className={linkBtn} disabled={offset + step >= total} onClick={() => onChange(offset + step)}>
        Next
      </button>
    </div>
  );
}

function Panel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <section aria-label={title} className="mt-6 rounded-xl border border-line bg-surface p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <h2 className="font-serif text-xl">{title}</h2>
        <button type="button" className={linkBtn} onClick={onClose}>
          Close
        </button>
      </div>
      {children}
    </section>
  );
}

function MissingView({ cov, onClose }: { cov: Coverage; onClose: () => void }) {
  const [ds, setDs] = useState(cov.datasets[0]?.key ?? "");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [res, setRes] = useState<MissingRes | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!ds) return;
    let stale = false;
    const t = setTimeout(() => {
      const p = new URLSearchParams({ dataset: ds, limit: "50", offset: String(offset) });
      if (q.trim()) p.set("q", q.trim());
      api<MissingRes>(`/data/coverage/missing?${p}`)
        .then((r) => !stale && (setRes(r), setError("")))
        .catch((e) => !stale && setError(err(e)));
    }, 200);
    return () => {
      stale = true;
      clearTimeout(t);
    };
  }, [ds, q, offset]);

  const shown = cov.datasets.find((d) => d.key === ds);
  return (
    <Panel title="What is missing" onClose={onClose}>
      <div role="tablist" aria-label="Dataset" className="flex flex-wrap gap-2">
        {cov.datasets.map((d) => (
          <button
            key={d.key}
            type="button"
            role="tab"
            aria-selected={d.key === ds}
            className={`rounded-full border px-3 py-1 text-xs focus-visible:outline-2 focus-visible:outline-brand ${d.key === ds ? "border-brand bg-brand text-brand-ink" : "border-line text-muted hover:text-ink"}`}
            onClick={() => {
              setDs(d.key);
              setOffset(0);
              setRes(null);
            }}
          >
            {d.label} {d.pct}%
          </button>
        ))}
      </div>
      <p className="mt-2 text-sm text-muted">
        {shown ? `${shown.have} of ${shown.total} funds have this.` : ""} Reference documents: {cov.reference_documents.loaded} loaded,{" "}
        {cov.reference_documents.note}.
      </p>
      {error ? <p className="mt-2 text-sm text-bad">{error}</p> : null}
      {res ? (
        <>
          <h3 className="mt-4 mb-1 text-sm text-muted">By AMC ({res.total_missing} funds missing)</h3>
          <Grid head={["AMC", "Missing", "Reason"]} rows={res.by_amc.map((a) => [a.amc, a.missing, a.reason])} />
          <h3 className="mt-4 mb-1 text-sm text-muted">Funds</h3>
          <input
            aria-label="Search missing funds"
            className={input}
            placeholder="Search fund name"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
          <div className="mt-2">
            <Grid head={["Fund", "AMC", "Category"]} rows={res.funds.map((f) => [f.name, f.amc, f.category])} />
          </div>
          <Pager offset={offset} step={50} total={q.trim() ? offset + res.funds.length + (res.funds.length === 50 ? 1 : 0) : res.total_missing} onChange={setOffset} />
        </>
      ) : null}
    </Panel>
  );
}

function Reader({ item, onClose }: { item: Item; onClose: () => void }) {
  const [table, setTable] = useState<TableData | null>(null);
  const [offset, setOffset] = useState(0);
  const [pv, setPv] = useState<Preview | null>(null);
  const [member, setMember] = useState<string | null>(null);
  const [sheet, setSheet] = useState<string | null>(null);
  const [error, setError] = useState("");
  const isFile = item.kind === "file";

  useEffect(() => {
    let stale = false;
    const base = apiPath(item.open_url);
    const url = isFile
      ? `${base}?limit=${PAGE_ROWS}${member ? `&member=${encodeURIComponent(member)}` : ""}${sheet ? `&sheet=${encodeURIComponent(sheet)}` : ""}`
      : `${base}?limit=${PAGE_ROWS}&offset=${offset}`;
    api<TableData | Preview>(url)
      .then((r) => {
        if (stale) return;
        setError("");
        if (isFile) setPv(r as Preview);
        else setTable(r as TableData);
      })
      .catch((e) => !stale && setError(err(e)));
    return () => {
      stale = true;
    };
  }, [item, isFile, offset, member, sheet]);

  const source = (table?.source ?? "") || String(item.location.split("Original: ")[1] ?? "");
  return (
    <Panel title={item.title} onClose={onClose}>
      {error ? <p className="text-sm text-bad">{error}</p> : null}
      {source ? (
        <p className="mb-2 text-sm text-muted">
          Source:{" "}
          {/^https?:/.test(source) ? (
            <a className="underline" href={source} target="_blank" rel="noreferrer">
              {source}
            </a>
          ) : (
            source
          )}
        </p>
      ) : null}
      {table ? (
        <>
          <p className="mb-2 text-sm text-muted">
            {table.total} rows, as of {fmt(table.as_of)}
          </p>
          <Grid head={table.columns} rows={table.rows} />
          <Pager offset={offset} step={PAGE_ROWS} total={table.total} onChange={setOffset} />
        </>
      ) : null}
      {pv?.members && !pv.rows ? (
        <>
          <p className="mb-2 text-sm text-muted">Zip archive. Choose a file to preview.</p>
          <ul className="max-h-72 space-y-1 overflow-auto text-sm">
            {pv.members.map((m) => (
              <li key={m.name}>
                <button type="button" className="text-left underline" onClick={() => { setMember(m.name); setSheet(null); }}>
                  {m.name}
                </button>{" "}
                <span className="text-muted">({Math.round(m.size / 1024)} KB)</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {pv?.rows ? (
        <>
          {member ? (
            <p className="mb-2 text-sm">
              <button type="button" className={linkBtn} onClick={() => { setMember(null); setSheet(null); setPv(null); }}>
                Back to archive
              </button>{" "}
              <span className="text-muted">{member}</span>
            </p>
          ) : null}
          <div role="tablist" aria-label="Sheets" className="mb-2 flex flex-wrap gap-2">
            {(pv.sheets ?? []).map((s) => (
              <button
                key={s}
                type="button"
                role="tab"
                aria-selected={s === pv.sheet}
                className={`rounded-full border px-3 py-1 text-xs focus-visible:outline-2 focus-visible:outline-brand ${s === pv.sheet ? "border-brand bg-brand text-brand-ink" : "border-line text-muted hover:text-ink"}`}
                onClick={() => setSheet(s)}
              >
                {s}
              </button>
            ))}
          </div>
          <p className="mb-2 text-sm text-muted">First {pv.rows.length} rows.</p>
          <Grid head={pv.rows[0]?.map((_, i) => String(i + 1)) ?? []} rows={pv.rows} />
        </>
      ) : null}
    </Panel>
  );
}

function BookCard({ it, onOpen }: { it: Item; onOpen: () => void }) {
  const d = it.details;
  const bits: [string, unknown][] = [
    ["As of", d.as_of],
    ["Rows", d.rows],
    ["Source", d.source],
    ["Size", typeof d.size_bytes === "number" ? `${Math.round(d.size_bytes / 1024)} KB` : null],
    ["Via", d.via],
    ["sha256", typeof d.sha256 === "string" ? d.sha256.slice(0, 12) : null],
  ];
  return (
    <li className="rounded-xl border border-line bg-surface p-4 text-sm">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-medium break-words">{it.title}</h3>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${it.available ? "bg-surface-2 text-ink" : "bg-bad/10 text-bad"}`}>
          {it.available ? "Available" : "Not available"}
        </span>
      </div>
      <p className="mt-1 text-muted">{it.amc}</p>
      <p className="mt-2">
        <span className="text-muted">Where it is: </span>
        <span className="break-words">{it.location}</span>
      </p>
      {it.available ? (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          {bits
            .filter(([, v]) => v != null && v !== "")
            .map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted">{k}</dt>
                <dd className="break-words">{String(v)}</dd>
              </div>
            ))}
        </dl>
      ) : (
        <p className="mt-2 text-muted">Reason: {fmt(d.note)}</p>
      )}
      {it.available ? (
        <div className="mt-3 flex items-center gap-4">
          <button type="button" className={btn} onClick={onOpen}>
            Open here
          </button>
          {it.kind === "file" ? (
            <a className={`${linkBtn} underline`} href={`${API_BASE}/data/files/${it.id}/download`}>
              Download
            </a>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/** Internal data library: a catalogue of what data we hold, where, and a reader for it. */
export default function DataPage() {
  const [cov, setCov] = useState<Coverage | null>(null);
  const [showMissing, setShowMissing] = useState(false);
  const [fund, setFund] = useState("");
  const [hits, setHits] = useState<FundHit[]>([]);
  const [cat, setCat] = useState("");
  const [amc, setAmc] = useState("");
  const [avail, setAvail] = useState("");
  const [items, setItems] = useState<Item[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [opened, setOpened] = useState<Item | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Coverage>("/data/coverage").then(setCov).catch((e) => setError(err(e)));
  }, []);

  useEffect(() => {
    const q = fund.trim();
    if (q.length < 2) return;
    let stale = false;
    const t = setTimeout(() => {
      api<FundHit[]>(`/funds/search?q=${encodeURIComponent(q)}`)
        .then((r) => !stale && setHits(r.slice(0, 10)))
        .catch(() => !stale && setHits([]));
    }, 250);
    return () => {
      stale = true;
      clearTimeout(t);
    };
  }, [fund]);

  const search = useCallback(
    async (off: number) => {
      setBusy(true);
      setError("");
      const p = new URLSearchParams({ limit: String(LIMIT), offset: String(off) });
      if (fund.trim()) p.set("q", fund.trim());
      if (cat === "files") p.set("kind", "file");
      else if (cat) p.set("dataset", cat);
      if (amc.trim()) p.set("amc", amc.trim());
      if (avail) p.set("available", avail === "yes" ? "true" : "false");
      try {
        const r = await api<{ total: number; items: Item[] }>(`/data/catalog?${p}`);
        setItems(r.items);
        setTotal(r.total);
        setOffset(off);
      } catch (e) {
        setError(err(e));
      } finally {
        setBusy(false);
      }
    },
    [fund, cat, amc, avail],
  );

  const headline = cov?.headline_pct;
  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-16">
      <header className="flex h-14 items-center justify-between">
        <div className="flex items-center gap-2">
          <Logo size={24} />
          <span className="font-serif text-lg">RA</span>
          <span className="rounded-full border border-line px-2 py-0.5 text-xs text-muted">Internal</span>
        </div>
        <Link href="/chat" className={linkBtn}>
          Chat
        </Link>
      </header>
      <main>
        <h1 className="mt-4 text-center font-serif text-3xl">Data library</h1>
        <div className="mt-4 text-center">
          <button
            type="button"
            disabled={!cov}
            aria-expanded={showMissing}
            className="rounded-xl px-4 py-2 hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-brand"
            onClick={() => setShowMissing((v) => !v)}
          >
            <span className="block font-serif text-5xl">{headline == null ? "..." : `${headline.toFixed(1)}%`}</span>
            <span className="block text-sm">
              {headline == null ? "" : `complete · ${(100 - headline).toFixed(1)}% pending`}
            </span>
          </button>
          <p className="text-xs text-muted">
            {cov ? `average across datasets for ${cov.universe.toLocaleString("en-IN")} funds. Click to see what is missing.` : ""}
          </p>
        </div>
        {showMissing && cov ? <MissingView cov={cov} onClose={() => setShowMissing(false)} /> : null}

        <form
          className="mt-8 grid grid-cols-1 gap-3 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            search(0);
          }}
        >
          <label className="text-sm text-muted">
            Fund (optional)
            <input className={`${input} mt-1`} list="fund-hits" value={fund} placeholder="Fund name" onChange={(e) => setFund(e.target.value)} />
            <datalist id="fund-hits">
              {hits.map((h) => (
                <option key={h.amfi_code} value={h.name} />
              ))}
            </datalist>
          </label>
          <label className="text-sm text-muted">
            Category
            <select className={`${input} mt-1`} value={cat} onChange={(e) => setCat(e.target.value)}>
              <option value="">All</option>
              {cov?.datasets.map((d) => (
                <option key={d.key} value={d.key}>
                  {d.label}
                </option>
              ))}
              <option value="files">Original files</option>
            </select>
          </label>
          <label className="text-sm text-muted">
            AMC
            <input className={`${input} mt-1`} value={amc} placeholder="AMC name" onChange={(e) => setAmc(e.target.value)} />
          </label>
          <label className="text-sm text-muted">
            Availability
            <select className={`${input} mt-1`} value={avail} onChange={(e) => setAvail(e.target.value)}>
              <option value="">All</option>
              <option value="yes">Available</option>
              <option value="no">Missing</option>
            </select>
          </label>
          <div className="sm:col-span-2">
            <button type="submit" className={btn} disabled={busy}>
              {busy ? "Searching..." : "Submit"}
            </button>
          </div>
        </form>
        {error ? <p className="mt-3 text-sm text-bad">{error}</p> : null}

        {opened ? <Reader key={opened.id} item={opened} onClose={() => setOpened(null)} /> : null}

        {items ? (
          <section className="mt-6" aria-label="Results">
            <p className="mb-2 text-sm text-muted">{total} books found.</p>
            <ul className="grid grid-cols-1 gap-3">
              {items.map((it) => (
                <BookCard key={`${it.kind}-${it.id}`} it={it} onOpen={() => setOpened(it)} />
              ))}
            </ul>
            <Pager offset={offset} step={LIMIT} total={total} onChange={search} />
          </section>
        ) : null}
      </main>
    </div>
  );
}

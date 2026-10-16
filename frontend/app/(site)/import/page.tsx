"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Badge, Button, Card, ErrorNote } from "@/components/ui";
import { api } from "@/lib/api";
import { rupees } from "@/lib/format";
import { useInvestorId } from "@/lib/investor";
import type { Investor } from "@/lib/types";

interface ImportResult {
  import_id: string;
  status: string;
  summary: { folios: number; schemes: number; transactions_added: number; statement_value: number; cas_type: string | null };
  exceptions: { type: string; scheme?: string; hint?: string }[];
}

const STEPS = [
  {
    title: "Request your CAS",
    body: "Ask CAMS or KFintech for a Consolidated Account Statement. Choose “Detailed”, period “Since inception”, and set a password you'll remember.",
    links: [
      { label: "CAMS CAS request", href: "https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement" },
      { label: "KFintech CAS request", href: "https://mfs.kfintech.com/investor/General/ConsolidatedAccountStatement" },
    ],
  },
  { title: "Wait for the email", body: "The PDF arrives in your registered email, usually within minutes. It covers every folio under your PAN." },
  { title: "Upload it here", body: "Upload the original PDF with its password. We read it in memory, never store the password, and delete the file once parsed." },
];

export default function ImportPage() {
  const router = useRouter();
  const [investorId, setInvestorId] = useInvestorId();
  const [name, setName] = useState("");
  const [pan, setPan] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file || !password || !consent) return;
    setBusy(true);
    setError(null);
    try {
      let id = investorId;
      if (!id) {
        const inv = await api<Investor>("/investors", { method: "POST", body: JSON.stringify({ display_name: name || "Investor", pan: pan || null }) });
        await api(`/investors/${inv.id}/consents`, { method: "POST", body: JSON.stringify({ purposes: ["import_analysis", "ai_processing"] }) });
        id = inv.id;
        setInvestorId(id);
      }
      const form = new FormData();
      form.append("file", file);
      form.append("password", password);
      setResult(await api<ImportResult>(`/investors/${id}/imports`, { method: "POST", body: form }));
      setPassword("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="font-serif text-3xl">Import your mutual funds</h1>
        <p className="text-muted text-sm mt-1">One statement brings in every fund you hold, from every app and AMC.</p>
      </div>

      <ol className="grid sm:grid-cols-3 gap-3">
        {STEPS.map((s, i) => (
          <li key={s.title} className="rounded-2xl border border-line bg-surface p-4 text-sm">
            <p className="font-medium">
              {i + 1}. {s.title}
            </p>
            <p className="text-muted mt-1">{s.body}</p>
            {s.links?.map((l) => (
              <a key={l.href} href={l.href} target="_blank" rel="noopener noreferrer" className="block text-brand font-medium mt-2">
                {l.label} ↗
              </a>
            ))}
          </li>
        ))}
      </ol>

      <Card title="Upload your CAS">
        <form onSubmit={submit} className="space-y-4">
          {!investorId ? (
            <div className="grid sm:grid-cols-2 gap-3">
              <label className="text-sm space-y-1">
                <span className="text-muted">Your first name</span>
                <input value={name} onChange={(e) => setName(e.target.value)} className="w-full rounded-xl border border-line bg-bg px-3 py-2" />
              </label>
              <label className="text-sm space-y-1">
                <span className="text-muted">PAN (checked against the statement)</span>
                <input
                  value={pan}
                  onChange={(e) => setPan(e.target.value.toUpperCase())}
                  maxLength={10}
                  placeholder="ABCDE1234F"
                  className="w-full rounded-xl border border-line bg-bg px-3 py-2 uppercase"
                />
              </label>
            </div>
          ) : null}
          <label className="text-sm space-y-1 block">
            <span className="text-muted">CAS PDF (max 10 MB)</span>
            <input type="file" accept="application/pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="block w-full text-sm" />
          </label>
          <label className="text-sm space-y-1 block">
            <span className="text-muted">PDF password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="off"
              className="w-full rounded-xl border border-line bg-bg px-3 py-2"
            />
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="mt-1" />
            <span className="text-muted">
              I consent to RA importing and analysing my mutual fund holdings, including AI processing of pseudonymised portfolio data (no name,
              PAN or phone). I can withdraw this consent anytime.
            </span>
          </label>
          {error ? <ErrorNote message={error} /> : null}
          <div className="flex flex-wrap gap-3">
            <Button type="submit" disabled={busy || !file || !password || !consent}>
              {busy ? "Reading your statement…" : "Import"}
            </Button>
            {!investorId ? (
              <Link href="/" className="text-sm text-muted self-center">
                or explore with a demo portfolio
              </Link>
            ) : null}
          </div>
        </form>
      </Card>

      {result ? (
        <Card title="Import complete">
          <p className="text-sm">
            {result.summary.schemes} funds across {result.summary.folios} folios, {result.summary.transactions_added} transactions, worth{" "}
            {rupees(result.summary.statement_value)} on the statement date.
          </p>
          {result.exceptions.length ? (
            <ul className="mt-3 space-y-2">
              {result.exceptions.map((x, i) => (
                <li key={i} className="text-sm">
                  <Badge tone="warn">{x.type.replace(/_/g, " ")}</Badge> <span className="text-muted">{x.scheme ?? ""} {x.hint ?? ""}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <Button className="mt-4" onClick={() => router.push("/dashboard")}>
            See my portfolio
          </Button>
        </Card>
      ) : null}
    </div>
  );
}

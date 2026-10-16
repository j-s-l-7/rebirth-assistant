"use client";

import { useState } from "react";
import AllocationBars from "@/components/AllocationBars";
import { Badge, Button, Card, ErrorNote, RequireInvestor, Spinner, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { dateIN } from "@/lib/format";
import type { Investor } from "@/lib/types";

interface Questionnaire {
  version: string;
  questions: { id: string; text: string; options: { id: string; label: string }[] }[];
}

interface Result {
  label: string;
  score: number;
  capacity_score: number;
  willingness_score: number;
  horizon_years: number;
  reasons: string[];
  target_allocation_pct: Record<string, number>;
  bands_pct: Record<string, [number, number]>;
}

function ProfileView({ investorId }: { investorId: string }) {
  const inv = useLoad(() => api<Investor>(`/investors/${investorId}`), [investorId]);
  const q = useLoad(() => api<Questionnaire>("/risk/questionnaire"), []);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if ((q.loading && !q.data) || (inv.loading && !inv.data)) return <Spinner />;
  if (q.error) return <ErrorNote message={q.error} />;
  const questions = q.data?.questions ?? [];
  const complete = questions.every((qq) => answers[qq.id]);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api<Result>(`/investors/${investorId}/risk-profile`, { method: "POST", body: JSON.stringify({ answers }) }));
      inv.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="font-serif text-3xl">Your risk profile</h1>
        <p className="text-sm text-muted mt-1">
          Eight questions, about two minutes. We look at your capacity to take risk and your willingness to, and use the more cautious of the
          two. It decides your target mix and which funds RA may suggest.
        </p>
        {inv.data?.risk_profile ? (
          <p className="text-sm mt-2">
            Current: <Badge tone="brand">{inv.data.risk_profile.label}</Badge>{" "}
            <span className="text-muted">valid till {dateIN(inv.data.risk_profile.valid_till)}</span>
          </p>
        ) : null}
      </div>

      {result ? (
        <Card title="Your result">
          <p className="font-serif text-2xl">{result.label}</p>
          <ul className="text-sm text-muted list-disc pl-5 mt-2 space-y-1">
            {result.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <div className="mt-5">
            <AllocationBars actual={result.target_allocation_pct} bands={result.bands_pct} />
            <p className="text-xs text-muted mt-2">Bars show your target mix; the dashed outline is the band we keep you within.</p>
          </div>
          <Button variant="ghost" className="mt-4" onClick={() => setResult(null)}>
            Retake
          </Button>
        </Card>
      ) : (
        <Card>
          <ol className="space-y-6">
            {questions.map((qq, i) => (
              <li key={qq.id}>
                <p className="font-medium mb-2">
                  {i + 1}. {qq.text}
                </p>
                <div className="flex flex-wrap gap-2">
                  {qq.options.map((o) => (
                    <button
                      key={o.id}
                      onClick={() => setAnswers({ ...answers, [qq.id]: o.id })}
                      className={`rounded-full border px-3 py-1.5 text-sm ${
                        answers[qq.id] === o.id ? "border-brand bg-brand-soft text-ink" : "border-line hover:bg-surface-2"
                      }`}
                      aria-pressed={answers[qq.id] === o.id}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ol>
          {error ? <div className="mt-4"><ErrorNote message={error} /></div> : null}
          <Button className="mt-6" onClick={submit} disabled={!complete || busy}>
            {busy ? "Scoring…" : "See my profile"}
          </Button>
        </Card>
      )}
    </div>
  );
}

export default function ProfilePage() {
  return <RequireInvestor>{(id) => <ProfileView investorId={id} />}</RequireInvestor>;
}

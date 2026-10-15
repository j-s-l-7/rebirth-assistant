"use client";

import { useState } from "react";
import RecoCard from "@/components/RecoCard";
import { Button, ErrorNote, RequireInvestor, Spinner, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { dateIN } from "@/lib/format";
import type { RecoCard as Card } from "@/lib/types";

interface Run {
  run_id: string;
  model_version: string;
  created_at: string;
  cards: Card[];
}

function RecoView({ investorId }: { investorId: string }) {
  const latest = useLoad(() => api<Run | null>(`/investors/${investorId}/recommendations/latest`), [investorId]);
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = run ?? latest.data ?? null;

  async function refresh() {
    setBusy(true);
    setError(null);
    try {
      setRun(await api<Run>(`/investors/${investorId}/recommendations/run`, { method: "POST" }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-serif text-3xl">Suggestions</h1>
          <p className="text-sm text-muted max-w-2xl">
            From RA&apos;s recommendation engine: every card passes a suitability check for your risk profile and shows its reasons and
            the estimated tax and exit-load impact. Mutual fund schemes only.
          </p>
        </div>
        <Button onClick={refresh} disabled={busy}>
          {busy ? "Running the engine…" : current ? "Refresh suggestions" : "Get suggestions"}
        </Button>
      </div>
      {error ? <ErrorNote message={error} /> : null}
      {latest.loading && !run ? <Spinner /> : null}
      {current ? (
        <>
          <p className="text-xs text-muted">
            Run {current.run_id.slice(0, 8)} · {current.model_version} · {dateIN(current.created_at)} · {current.cards.length} suggestion
            {current.cards.length === 1 ? "" : "s"}
          </p>
          {current.cards.length ? (
            <div className="grid md:grid-cols-2 gap-4">
              {current.cards.map((c, i) => (
                <RecoCard key={c.id ?? i} card={c} />
              ))}
            </div>
          ) : (
            <p className="text-muted">Nothing to change right now. Your portfolio fits your profile.</p>
          )}
        </>
      ) : !latest.loading ? (
        <p className="text-muted">No suggestions yet. Run the engine to see what RA would change.</p>
      ) : null}
      <p className="text-xs text-muted">
        We earn trail commission from fund houses on Regular plans; see the commission disclosure for scheme-wise rates. Past performance may or
        may not be sustained in future.
      </p>
    </div>
  );
}

export default function RecommendationsPage() {
  return <RequireInvestor>{(id) => <RecoView investorId={id} />}</RequireInvestor>;
}

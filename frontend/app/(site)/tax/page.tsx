"use client";

import { Card, ErrorNote, RequireInvestor, Spinner, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { dateIN, rupees, tone } from "@/lib/format";

interface Tax {
  financial_year: string;
  realised_this_fy: { short_term_equity_gain: number; long_term_gain: number; of_which_equity_long_term: number; slab_taxed_gain: number; estimated_tax: number };
  equity_ltcg_exemption: { limit: number; used: number; remaining: number };
  unrealised_if_sold_today: { short_term_gain: number; long_term_gain: number; slab_taxed_gain: number };
  ltcg_harvest_opportunity: number;
  lots_turning_long_term_in_60_days: { scheme: string; long_term_from: string; unrealised_gain: number }[];
  rates_used: { equity_stcg_pct: number; equity_ltcg_pct: number; slab_pct_assumed: number };
  label: string;
}

function Row({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex justify-between py-2 border-b border-line last:border-0 text-sm">
      <span className="text-muted">{label}</span>
      <span className={`num ${tone(value)}`}>{rupees(value)}</span>
    </div>
  );
}

function TaxView({ investorId }: { investorId: string }) {
  const { data, error, loading } = useLoad(() => api<Tax>(`/investors/${investorId}/tax`), [investorId]);
  if (loading) return <Spinner label="Working through your tax position" />;
  if (error) return <ErrorNote message={error} />;
  if (!data) return null;
  const ex = data.equity_ltcg_exemption;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-serif text-3xl">Tax · {data.financial_year}</h1>
        <p className="text-sm text-muted mt-1">
          From your own transactions, FIFO. Equity: short-term {data.rates_used.equity_stcg_pct}%, long-term {data.rates_used.equity_ltcg_pct}% above
          ₹1.25 lakh a year. Slab-taxed gains use {data.rates_used.slab_pct_assumed}%. {data.label}
        </p>
      </div>
      <div className="grid md:grid-cols-2 gap-6">
        <Card title="Booked this year">
          <Row label="Short-term equity gains" value={data.realised_this_fy.short_term_equity_gain} />
          <Row label="Long-term gains" value={data.realised_this_fy.long_term_gain} />
          <Row label="Slab-taxed gains" value={data.realised_this_fy.slab_taxed_gain} />
          <Row label="Estimated tax" value={-data.realised_this_fy.estimated_tax} />
        </Card>
        <Card title="₹1.25 lakh equity LTCG exemption">
          <div className="h-3 rounded-full bg-surface-2 overflow-hidden">
            <div className="h-full bg-brand" style={{ width: `${Math.min(100, (ex.used / ex.limit) * 100)}%` }} />
          </div>
          <p className="text-sm mt-2 num">
            Used {rupees(ex.used)} · remaining <span className="font-medium">{rupees(ex.remaining)}</span>
          </p>
          {data.ltcg_harvest_opportunity > 0 ? (
            <p className="text-sm text-muted mt-3">
              You could book up to {rupees(data.ltcg_harvest_opportunity)} of long-term equity gains this year without tax and reinvest, resetting
              your cost higher. Worth doing before 31 March.
            </p>
          ) : null}
        </Card>
        <Card title="If you sold everything today">
          <Row label="Short-term equity gains" value={data.unrealised_if_sold_today.short_term_gain} />
          <Row label="Long-term gains" value={data.unrealised_if_sold_today.long_term_gain} />
          <Row label="Slab-taxed gains" value={data.unrealised_if_sold_today.slab_taxed_gain} />
        </Card>
        <Card title="Turning long-term soon">
          {data.lots_turning_long_term_in_60_days.length ? (
            <ul className="space-y-2 text-sm">
              {data.lots_turning_long_term_in_60_days.map((l, i) => (
                <li key={i} className="flex justify-between gap-3">
                  <span>
                    {l.scheme}
                    <span className="block text-xs text-muted">long-term from {dateIN(l.long_term_from)}</span>
                  </span>
                  <span className="num text-good">{rupees(l.unrealised_gain)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">No units cross into long-term in the next 60 days.</p>
          )}
        </Card>
      </div>
    </div>
  );
}

export default function TaxPage() {
  return <RequireInvestor>{(id) => <TaxView investorId={id} />}</RequireInvestor>;
}

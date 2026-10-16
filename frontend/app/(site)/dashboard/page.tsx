"use client";

import Link from "next/link";
import AlertList from "@/components/AlertList";
import AllocationBars from "@/components/AllocationBars";
import Dimensions from "@/components/Dimensions";
import HoldingsTable from "@/components/HoldingsTable";
import ScoreRing from "@/components/ScoreRing";
import { Card, ErrorNote, RequireInvestor, Spinner, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { compact, dateIN, pct, rupees, signedPct, tone } from "@/lib/format";
import type { Dashboard, Investor } from "@/lib/types";

function Kpi({ label, value, sub, subTone }: { label: string; value: string; sub?: string; subTone?: string }) {
  return (
    <div className="rounded-2xl border border-line bg-surface p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className="font-serif text-2xl num mt-1">{value}</div>
      {sub ? <div className={`text-sm num mt-0.5 ${subTone ?? "text-muted"}`}>{sub}</div> : null}
    </div>
  );
}

function DashboardView({ investorId }: { investorId: string }) {
  const inv = useLoad(() => api<Investor>(`/investors/${investorId}`), [investorId]);
  const { data, error, loading } = useLoad(() => api<Dashboard>(`/investors/${investorId}/dashboard`), [investorId]);
  if (loading) return <Spinner label="Reading your portfolio" />;
  if (error) return <ErrorNote message={error} />;
  if (!data) return null;
  const o = data.overview;
  if (!o.holdings_count) {
    return (
      <div className="max-w-md mx-auto text-center py-16 space-y-3">
        <p className="font-serif text-2xl">No funds yet</p>
        <p className="text-muted">Import your CAS statement to see your portfolio, your RA Score and suggestions.</p>
        <Link className="inline-flex rounded-full bg-brand text-brand-ink px-4 py-2 text-sm font-medium" href="/import">
          Import my funds
        </Link>
      </div>
    );
  }
  const balance = data.score?.dimensions.find((d) => d.key === "balance");
  const bands = balance?.inputs?.bands as Record<string, [number, number]> | undefined;
  const target = balance?.inputs?.target as Record<string, number> | undefined;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-serif text-3xl">Hello{inv.data ? `, ${inv.data.display_name}` : ""}</h1>
          <p className="text-sm text-muted">
            Values as of {dateIN(o.nav_data_as_of ?? o.as_of)} (latest AMFI NAVs)
            {inv.data?.risk_profile ? ` · ${inv.data.risk_profile.label} profile` : ""}
          </p>
        </div>
        <Link href="/chat" className="rounded-full bg-brand text-brand-ink px-4 py-2 text-sm font-medium">
          Ask RA about this
        </Link>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Current value" value={compact(o.current_value)} sub={`Day ${o.day_change >= 0 ? "+" : ""}${rupees(o.day_change)}`} subTone={tone(o.day_change)} />
        <Kpi label="Invested" value={compact(o.invested)} sub={`${o.active_sips} active SIPs · ${rupees(o.monthly_sip_total)}/mo`} />
        <Kpi label="Gain" value={compact(o.gain)} sub={signedPct(o.gain_pct)} subTone={tone(o.gain)} />
        <Kpi label="XIRR" value={pct(o.xirr_pct)} sub="Annualised, on your cashflows" />
      </div>

      <div className="grid lg:grid-cols-5 gap-6">
        <Card title="RA Score" className="lg:col-span-3">
          {data.score ? (
            <div className="flex flex-col sm:flex-row gap-6">
              <div className="flex flex-col items-center gap-3 sm:w-48 shrink-0">
                <ScoreRing score={data.score.score} grade={data.score.grade} />
                <p className="text-xs text-muted text-center">{data.score.coverage}. Higher is better.</p>
                {data.score.focus.length ? (
                  <div className="text-sm rounded-xl bg-surface-2 p-3 w-full">
                    <p className="font-medium mb-1">Focus on</p>
                    {data.score.focus.map((f) => (
                      <p key={f.dimension} className="text-muted">
                        {f.dimension} ({f.score})
                      </p>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="flex-1 min-w-0">
                <Dimensions dims={data.score.dimensions} />
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">Score unavailable.</p>
          )}
        </Card>
        <div className="lg:col-span-2 space-y-6">
          <Card title="Mix vs your target">
            <AllocationBars actual={o.allocation_pct} bands={bands} target={target} />
            {!bands ? (
              <p className="text-sm text-muted mt-3">
                <Link href="/profile" className="text-brand font-medium">
                  Take the risk profile
                </Link>{" "}
                to see your personal target bands.
              </p>
            ) : null}
          </Card>
          <Card title="For your attention">
            <AlertList alerts={data.alerts} />
          </Card>
        </div>
      </div>

      <Card
        title={`Your funds (${o.holdings_count})`}
        action={
          <Link href="/recommendations" className="text-sm text-brand font-medium">
            See suggestions →
          </Link>
        }
      >
        <HoldingsTable holdings={data.holdings} />
        <p className="text-xs text-muted mt-4">
          &ldquo;vs benchmark&rdquo; replays your own investments into the category&apos;s index fund and compares XIRR. Tiers rank a fund
          within its SEBI category on consistency, risk-adjusted return and downside protection. Past performance may or may
          not be sustained in future.
        </p>
      </Card>
    </div>
  );
}

export default function DashboardPage() {
  return <RequireInvestor>{(id) => <DashboardView investorId={id} />}</RequireInvestor>;
}

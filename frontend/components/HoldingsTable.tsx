import { Badge } from "@/components/ui";
import { compact, pct, rupees, signedPct, tone } from "@/lib/format";
import type { Holding } from "@/lib/types";

const TIER_TONE = { core: "good", satellite: "info", watch: "warn", exit: "bad", new: "neutral" } as const;

function Tier({ tier }: { tier?: string | null }) {
  if (!tier) return null;
  return <Badge tone={TIER_TONE[tier as keyof typeof TIER_TONE] ?? "neutral"}>{tier}</Badge>;
}

function VsBenchmark({ h }: { h: Holding }) {
  if (!h.vs_benchmark) return <span className="text-muted">—</span>;
  const a = h.vs_benchmark.alpha_pp;
  return (
    <span className={tone(a)} title={`Same cashflows in ${h.vs_benchmark.benchmark}: ${h.vs_benchmark.benchmark_xirr_pct}% XIRR`}>
      {a > 0 ? "+" : ""}
      {a.toFixed(1)} pts/yr
    </span>
  );
}

export default function HoldingsTable({ holdings }: { holdings: Holding[] }) {
  return (
    <>
      <div className="hidden md:block overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted uppercase tracking-wide">
            <tr className="border-b border-line">
              <th className="py-2 pr-3 font-medium">Fund</th>
              <th className="py-2 px-3 font-medium text-right">Value</th>
              <th className="py-2 px-3 font-medium text-right">Gain</th>
              <th className="py-2 px-3 font-medium text-right">XIRR</th>
              <th className="py-2 px-3 font-medium text-right">vs benchmark</th>
              <th className="py-2 pl-3 font-medium">Signals</th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((h) => (
              <tr key={h.amfi_code} className="border-b border-line last:border-0 align-top">
                <td className="py-3 pr-3">
                  <div className="font-medium leading-snug">{h.name}</div>
                  <div className="text-xs text-muted">
                    {h.category} · {h.riskometer} risk · {pct(h.weight_pct)} of portfolio
                  </div>
                </td>
                <td className="py-3 px-3 text-right num">{compact(h.value)}</td>
                <td className={`py-3 px-3 text-right num ${tone(h.gain)}`}>
                  {rupees(h.gain)}
                  <div className="text-xs">{signedPct(h.gain_pct)}</div>
                </td>
                <td className="py-3 px-3 text-right num">{pct(h.xirr_pct)}</td>
                <td className="py-3 px-3 text-right num">
                  <VsBenchmark h={h} />
                </td>
                <td className="py-3 pl-3">
                  <div className="flex flex-wrap gap-1">
                    <Tier tier={h.metrics?.tier} />
                    {h.sip ? <Badge tone={h.sip.status === "active" ? "good" : "warn"}>SIP {h.sip.status.replace("_", " ")}</Badge> : null}
                    {h.suitability && !h.suitability.pass ? <Badge tone="bad">Above your risk</Badge> : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="md:hidden space-y-3">
        {holdings.map((h) => (
          <li key={h.amfi_code} className="rounded-xl border border-line p-3">
            <div className="font-medium leading-snug">{h.name}</div>
            <div className="text-xs text-muted mb-2">
              {h.category} · {pct(h.weight_pct)} of portfolio
            </div>
            <div className="grid grid-cols-3 gap-2 text-sm num">
              <div>
                <div className="text-xs text-muted">Value</div>
                {compact(h.value)}
              </div>
              <div>
                <div className="text-xs text-muted">Gain</div>
                <span className={tone(h.gain)}>{signedPct(h.gain_pct)}</span>
              </div>
              <div>
                <div className="text-xs text-muted">vs bench</div>
                <VsBenchmark h={h} />
              </div>
            </div>
            <div className="flex flex-wrap gap-1 mt-2">
              <Tier tier={h.metrics?.tier} />
              {h.suitability && !h.suitability.pass ? <Badge tone="bad">Above your risk</Badge> : null}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

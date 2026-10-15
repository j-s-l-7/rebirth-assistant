"use client";

import Link from "next/link";
import { useState } from "react";
import { Badge } from "@/components/ui";
import { dateIN, rupees } from "@/lib/format";
import type { RecoCard as Card } from "@/lib/types";

const ACTION_LABEL: Record<string, string> = {
  SWITCH: "Switch",
  STOP_SIP: "Redirect SIP",
  START_SIP: "Start SIP",
  REDIRECT_SIP: "Redirect new money",
  STP: "Systematic transfer",
  REVIEW: "Review",
  COMPLETE_RISK_PROFILE: "Next step",
};

export default function RecoCard({ card, compact = false }: { card: Card; compact?: boolean }) {
  const [why, setWhy] = useState(false);
  const tax = card.tax_and_exit_load;
  return (
    <article className="rounded-2xl border border-line bg-surface p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <Badge tone={card.priority <= 20 ? "warn" : "brand"}>{ACTION_LABEL[card.action] ?? card.action}</Badge>
          <h3 className="font-medium leading-snug">{card.title}</h3>
        </div>
        {card.amount_inr || card.sip_amount_inr ? (
          <div className="text-right num shrink-0">
            {card.amount_inr ? <div className="font-medium">{rupees(card.amount_inr)}</div> : null}
            {card.sip_amount_inr ? <div className="text-sm text-muted">{rupees(card.sip_amount_inr)}/month</div> : null}
          </div>
        ) : null}
      </div>

      {card.from_scheme || card.to_scheme ? (
        <div className="text-sm rounded-xl bg-surface-2 p-3 space-y-1">
          {card.from_scheme ? (
            <div>
              <span className="text-muted">From </span>
              {card.from_scheme.name}
            </div>
          ) : null}
          {card.to_scheme ? (
            <div>
              <span className="text-muted">To </span>
              {card.to_scheme.name}
              <span className="text-muted">
                {" "}
                · {card.to_scheme.category}
                {card.to_scheme.riskometer ? ` · ${card.to_scheme.riskometer} risk` : ""}
              </span>
            </div>
          ) : null}
        </div>
      ) : null}

      <ul className="text-sm text-muted list-disc pl-5 space-y-1">
        {card.rationale.slice(0, compact ? 2 : undefined).map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>

      {(card.suitability || tax) && (
        <button className="text-sm text-brand font-medium" onClick={() => setWhy(!why)} aria-expanded={why}>
          {why ? "Hide details" : "Tax & suitability details"}
        </button>
      )}
      {why ? (
        <div className="text-sm space-y-2 border-t border-line pt-3">
          {card.suitability ? (
            <p>
              <Badge tone={card.suitability.pass ? "good" : "bad"}>{card.suitability.pass ? "Suits your profile" : "Above your profile"}</Badge>{" "}
              <span className="text-muted">
                {card.suitability.scheme_category} for a {card.suitability.investor_bucket} investor
              </span>
            </p>
          ) : null}
          {tax ? (
            <div className="text-muted space-y-1">
              <p className="num">
                Estimated gain {rupees(tax.est_gain_inr)} · tax {rupees(tax.est_tax_inr)} · exit load {rupees(tax.exit_load_inr)}{" "}
                <span className="text-xs">({tax.label})</span>
              </p>
              {tax.sequencing ? <p>{tax.sequencing}</p> : null}
              {tax.exemption_note ? <p className="text-xs">{tax.exemption_note}</p> : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
        <span className="text-xs text-muted">
          Data as of {dateIN(card.data_as_of)}
          {card.expires_at ? ` · valid till ${dateIN(card.expires_at)}` : ""}
        </span>
        {card.action === "COMPLETE_RISK_PROFILE" ? (
          <Link href="/profile" className="text-sm font-medium text-brand">
            Take the risk profile →
          </Link>
        ) : (
          <span className="text-xs text-muted" title="Order placement opens once the ARN and BSE StAR MF onboarding are live">
            One-tap ordering coming soon
          </span>
        )}
      </div>
    </article>
  );
}

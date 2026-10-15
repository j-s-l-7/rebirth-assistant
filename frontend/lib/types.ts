export interface Investor {
  id: string;
  display_name: string;
  pan_masked: string | null;
  is_demo: boolean;
  tax_slab_pct: number | null;
  risk_profile: { bucket: string; label: string; horizon_years: number; valid_till: string } | null;
}

export interface Overview {
  as_of: string;
  nav_data_as_of: string | null;
  current_value: number;
  invested: number;
  gain: number;
  gain_pct: number | null;
  xirr_pct: number | null;
  day_change: number;
  holdings_count: number;
  allocation_pct: Record<"equity" | "debt" | "gold", number>;
  cash_like_pct: number;
  by_category_pct: Record<string, number>;
  by_amc_pct: Record<string, number>;
  realised_this_fy: Record<string, number>;
  active_sips: number;
  monthly_sip_total: number;
}

export interface Holding {
  amfi_code: number;
  name: string;
  amc: string;
  category: string;
  asset_group: string;
  plan: string;
  riskometer: string;
  units: number;
  nav: number;
  nav_date: string | null;
  value: number;
  invested: number;
  gain: number;
  gain_pct: number | null;
  xirr_pct: number | null;
  weight_pct: number;
  sip: { amount: number; status: string; missed_last_12m: number; next_due: string | null } | null;
  metrics: { tier?: string | null; composite_score?: number | null; cagr_3y?: number | null; consistency?: number | null } | null;
  vs_benchmark: { benchmark: string; alpha_pp: number; benchmark_xirr_pct: number; fund_xirr_pct: number; years: number } | null;
  suitability: { pass: boolean; reason: string | null } | null;
  typical_exit_load: string;
}

export interface Dimension {
  key: string;
  label: string;
  weight: number;
  score: number | null;
  status: "scored" | "not_scored";
  summary: string;
  inputs: Record<string, unknown>;
  improve: string[];
}

export interface Score {
  version: string;
  as_of: string;
  score: number | null;
  grade: string | null;
  dimensions: Dimension[];
  coverage: string;
  focus: { dimension: string; score: number; first_step: string }[];
}

export interface Alert {
  type: string;
  severity: "action" | "heads_up" | "info";
  title: string;
  body: string;
  due_date?: string;
}

export interface SchemeRef {
  amfi_code: number;
  name: string;
  category: string;
  riskometer: string | null;
  plan: string;
}

export interface RecoCard {
  id?: string;
  finding: string;
  priority: number;
  action: string;
  title: string;
  from_scheme: SchemeRef | null;
  to_scheme: SchemeRef | null;
  amount_inr: number | null;
  sip_amount_inr: number | null;
  rationale: string[];
  suitability: { scheme_category: string; scheme_riskometer: string | null; investor_bucket: string; pass: boolean } | null;
  tax_and_exit_load: {
    est_gain_inr: number;
    est_tax_inr: number;
    exit_load_inr: number;
    sequencing: string | null;
    exemption_note?: string | null;
    label: string;
  } | null;
  data_as_of: string;
  expires_at?: string;
  disclosures: string[];
}

export interface Dashboard {
  overview: Overview;
  holdings: Holding[];
  score: Score | null;
  alerts: Alert[];
  risk_bucket: string | null;
}

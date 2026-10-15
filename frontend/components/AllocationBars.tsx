const ASSETS: { key: "equity" | "debt" | "gold"; label: string; color: string }[] = [
  { key: "equity", label: "Equity", color: "bg-brand" },
  { key: "debt", label: "Debt", color: "bg-info" },
  { key: "gold", label: "Gold & silver", color: "bg-warn" },
];

export default function AllocationBars({
  actual,
  bands,
  target,
}: {
  actual: Record<string, number>;
  bands?: Record<string, [number, number]>;
  target?: Record<string, number>;
}) {
  return (
    <div className="space-y-4">
      {ASSETS.map((a) => {
        const v = actual[a.key] ?? 0;
        const band = bands?.[a.key];
        const outside = band ? v < band[0] || v > band[1] : false;
        return (
          <div key={a.key}>
            <div className="flex justify-between text-sm mb-1.5">
              <span>{a.label}</span>
              <span className="num">
                <span className={outside ? "text-warn font-medium" : ""}>{v.toFixed(1)}%</span>
                {band ? (
                  <span className="text-muted">
                    {" "}
                    · band {band[0]}–{band[1]}%{target ? `, target ${target[a.key]}%` : ""}
                  </span>
                ) : null}
              </span>
            </div>
            <div className="relative h-2.5 rounded-full bg-surface-2">
              {band ? (
                <div
                  className="absolute inset-y-0 rounded-full border border-dashed border-muted/60"
                  style={{ left: `${band[0]}%`, width: `${band[1] - band[0]}%` }}
                  title="Your target band"
                />
              ) : null}
              <div className={`absolute inset-y-0 left-0 rounded-full ${a.color}`} style={{ width: `${Math.min(100, v)}%` }} />
            </div>
          </div>
        );
      })}
      {bands ? <p className="text-xs text-muted">Dashed outline: the band for your risk profile.</p> : null}
    </div>
  );
}

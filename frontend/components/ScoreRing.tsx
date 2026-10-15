export default function ScoreRing({ score, grade, size = 148 }: { score: number | null; grade: string | null; size?: number }) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const value = score ?? 0;
  const color = value >= 80 ? "var(--good)" : value >= 65 ? "var(--accent)" : value >= 50 ? "var(--warn)" : "var(--bad)";
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg viewBox="0 0 120 120" width={size} height={size} role="img" aria-label={`RA Score ${score ?? "not available"} out of 100`}>
        <circle cx="60" cy="60" r={r} fill="none" stroke="var(--surface-2)" strokeWidth="10" />
        <circle
          cx="60"
          cy="60"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={`${(value / 100) * c} ${c}`}
          transform="rotate(-90 60 60)"
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="font-serif text-4xl num">{score ?? "—"}</span>
        <span className="text-xs text-muted">{grade ?? "Not scored"}</span>
      </div>
    </div>
  );
}

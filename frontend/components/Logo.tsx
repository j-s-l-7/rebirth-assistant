export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="17" r="11" fill="none" stroke="currentColor" strokeWidth="3" />
      <path d="M16 17 m-5 0 a5 5 0 1 0 5 -5" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
      <circle cx="16" cy="3.5" r="2.5" className="fill-accent" />
    </svg>
  );
}

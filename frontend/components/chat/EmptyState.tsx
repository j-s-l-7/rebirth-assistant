"use client";

const EXAMPLES = [
  "How does Parag Parikh Flexi Cap compare with other flexi cap funds?",
  "HDFC Mid Cap vs Motilal Oswal Midcap — which is more consistent?",
  "Is ICICI Prudential Large Cap well diversified?",
];

export default function EmptyState({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className="ra-fade-in my-auto flex flex-col items-center py-10 text-center">
      <h2 className="font-serif text-2xl text-balance sm:text-3xl">Ask about any mutual fund</h2>
      <ul className="mt-6 flex max-w-xl flex-col items-center gap-0.5">
        {EXAMPLES.map((q) => (
          <li key={q}>
            <button
              type="button"
              onClick={() => onPick(q)}
              className="rounded-xl px-3 py-2 text-sm leading-snug text-balance text-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand"
            >
              {q}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

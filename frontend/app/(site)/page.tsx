"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Logo } from "@/components/Nav";
import { Button, ErrorNote } from "@/components/ui";
import { api } from "@/lib/api";
import { useInvestorId } from "@/lib/investor";
import type { Investor } from "@/lib/types";

const STEPS = [
  {
    title: "It starts with your funds, and your whole story",
    body: "Import every mutual fund you own from one CAS statement. Then the things a screener never asks: when you need the money and how much risk you can actually carry.",
  },
  {
    title: "It taps you on the shoulder",
    body: "A lot about to turn long-term, an exit load about to end, a SIP that missed its date, a mix that drifted. You hear before you'd think to ask.",
  },
  {
    title: "Every suggestion shows its working",
    body: "Each card carries its reasons, the suitability check, the tax and exit-load impact and the data date. A human expert can review anything, free.",
  },
  {
    title: "It's yours, and only yours",
    body: "Your PAN is encrypted, never shown to the AI and never sold or shared. Withdraw consent and your data goes.",
  },
];

export default function Home() {
  const router = useRouter();
  const [investorId, setInvestorId] = useInvestorId();
  const [busy, setBusy] = useState<"demo" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function startDemo() {
    setBusy("demo");
    setError(null);
    try {
      const inv = await api<Investor>("/investors/demo", { method: "POST" });
      setInvestorId(inv.id);
      router.push("/dashboard");
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  return (
    <div className="max-w-2xl mx-auto py-6 sm:py-10">
      <div className="flex flex-col items-center text-center gap-4">
        <span className="text-ink">
          <Logo size={56} />
        </span>
        <h1 className="font-serif text-3xl sm:text-4xl leading-tight">RA is your always-on mutual fund companion</h1>
        <p className="text-muted max-w-lg">
          See what you own, what&apos;s working and what isn&apos;t, then act on suitable, explained suggestions. Answers in seconds.
        </p>
      </div>

      <div className="flex items-center gap-3 my-8 text-xs text-muted">
        <span className="h-px flex-1 bg-line" /> How it works <span className="h-px flex-1 bg-line" />
      </div>

      <ol className="space-y-6">
        {STEPS.map((s, i) => (
          <li key={s.title} className="flex gap-4">
            <span className="h-8 w-8 shrink-0 rounded-full bg-brand-soft text-ink flex items-center justify-center text-sm font-medium">{i + 1}</span>
            <div>
              <p className="font-medium">{s.title}</p>
              <p className="text-sm text-muted mt-1">{s.body}</p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-10 space-y-3">
        {error ? <ErrorNote message={error} /> : null}
        {investorId ? (
          <div className="flex flex-col sm:flex-row gap-3">
            <Button className="flex-1 py-3" onClick={() => router.push("/dashboard")}>
              Continue to my portfolio
            </Button>
            <Button variant="ghost" className="flex-1 py-3" onClick={() => setInvestorId(null)}>
              Start over
            </Button>
          </div>
        ) : (
          <div className="flex flex-col sm:flex-row gap-3">
            <Button className="flex-1 py-3" onClick={() => router.push("/import")}>
              Import my mutual funds
            </Button>
            <Button variant="ghost" className="flex-1 py-3" onClick={startDemo} disabled={busy !== null}>
              {busy === "demo" ? "Building demo portfolio…" : "Explore with a demo portfolio"}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

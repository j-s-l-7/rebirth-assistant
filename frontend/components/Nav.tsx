"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { adoptInvestorFromLink, useInvestorId } from "@/lib/investor";

const LINKS = [
  { href: "/dashboard", label: "Portfolio" },
  { href: "/chat", label: "Ask RA" },
  { href: "/recommendations", label: "Suggestions" },
  { href: "/tax", label: "Tax" },
  { href: "/import", label: "Import" },
  { href: "/profile", label: "Risk profile" },
];

export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="17" r="11" fill="none" stroke="currentColor" strokeWidth="3" />
      <path d="M16 17 m-5 0 a5 5 0 1 0 5 -5" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
      <circle cx="16" cy="3.5" r="2.5" className="fill-accent" />
    </svg>
  );
}

export default function Nav() {
  const pathname = usePathname();
  const [investorId] = useInvestorId();

  // Shareable demo links: any page accepts ?investor=<id> and remembers it.
  useEffect(() => {
    adoptInvestorFromLink();
  }, []);
  return (
    <header className="border-b border-line bg-surface/80 backdrop-blur sticky top-0 z-20">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 h-14 flex items-center gap-4">
        <Link href="/" className="flex items-center gap-2 text-ink shrink-0">
          <Logo />
          <span className="font-serif text-lg">RA</span>
        </Link>
        {investorId ? (
          <nav className="flex gap-1 overflow-x-auto text-sm -mx-1" aria-label="Main">
            {LINKS.map((l) => {
              const active = pathname?.startsWith(l.href);
              return (
                <Link
                  key={l.href}
                  href={l.href}
                  className={`px-3 py-1.5 rounded-full whitespace-nowrap transition-colors ${
                    active ? "bg-brand-soft text-ink font-medium" : "text-muted hover:text-ink"
                  }`}
                >
                  {l.label}
                </Link>
              );
            })}
          </nav>
        ) : null}
      </div>
    </header>
  );
}

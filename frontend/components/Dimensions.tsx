"use client";

import { useState } from "react";
import type { Dimension } from "@/lib/types";

function barColor(score: number) {
  return score >= 80 ? "bg-good" : score >= 65 ? "bg-accent" : score >= 50 ? "bg-warn" : "bg-bad";
}

export default function Dimensions({ dims }: { dims: Dimension[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <ul className="divide-y divide-line">
      {dims.map((d) => {
        const isOpen = open === d.key;
        return (
          <li key={d.key} className="py-3">
            <button className="w-full text-left" onClick={() => setOpen(isOpen ? null : d.key)} aria-expanded={isOpen}>
              <div className="flex items-center justify-between gap-3">
                <span className="font-medium">
                  {d.label} <span className="text-xs text-muted font-normal">· {d.weight}%</span>
                </span>
                <span className="num text-sm">{d.status === "scored" ? d.score : "Not scored"}</span>
              </div>
              <div className="mt-2 h-1.5 rounded-full bg-surface-2 overflow-hidden">
                {d.status === "scored" && d.score != null ? (
                  <div className={`h-full rounded-full ${barColor(d.score)}`} style={{ width: `${d.score}%` }} />
                ) : null}
              </div>
              <p className="mt-2 text-sm text-muted">{d.summary}</p>
            </button>
            {isOpen && d.improve.length > 0 ? (
              <div className="mt-3 rounded-xl bg-surface-2 p-3 text-sm">
                <p className="font-medium mb-1">What would raise it</p>
                <ul className="list-disc pl-5 space-y-1 text-muted">
                  {d.improve.map((i) => (
                    <li key={i}>{i}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

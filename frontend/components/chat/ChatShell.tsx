"use client";

import { useEffect, type ReactNode } from "react";

/** The full-height white chat window. While it is on screen the page behind it is white too and
 *  never scrolls (the message list does). The class lives on <html> only while this is mounted:
 *  Next keeps the previous route hidden in the DOM after navigating, so CSS keyed on the markup
 *  alone (":has(.shell)") would leak onto the other pages. Effects, unlike markup, are torn down. */
export default function ChatShell({ children }: { children: ReactNode }) {
  useEffect(() => {
    const root = document.documentElement;
    root.classList.add("ra-chat");
    return () => root.classList.remove("ra-chat");
  }, []);

  return <div className="h-dvh overflow-hidden bg-surface text-ink">{children}</div>;
}

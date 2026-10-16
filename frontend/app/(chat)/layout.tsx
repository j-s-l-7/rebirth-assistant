import type { Viewport } from "next";
import ChatShell from "@/components/chat/ChatShell";

// On Android Chrome the on-screen keyboard shrinks the page (so the pinned input stays visible)
// instead of covering it. Browsers that don't know this hint ignore it.
export const viewport: Viewport = { interactiveWidget: "resizes-content" };

/** Standalone chat: no nav, no footer, just a white full-height window. */
export default function ChatLayout({ children }: { children: React.ReactNode }) {
  return <ChatShell>{children}</ChatShell>;
}

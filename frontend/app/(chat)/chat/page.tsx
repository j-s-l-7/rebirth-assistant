"use client";

import { useEffect, useRef } from "react";
import Composer, { focusInput } from "@/components/chat/Composer";
import EmptyState from "@/components/chat/EmptyState";
import Message from "@/components/chat/Message";
import { useChat } from "@/components/chat/useChat";
import { useStickToBottom } from "@/components/chat/useStickToBottom";
import { Logo } from "@/components/Logo";

/** A plain chat window: ask about any mutual fund, get a short answer. Nothing else. */
export default function ChatPage() {
  const { messages, booting, ready, busy, send, retry, newChat } = useChat();
  const { scroller, content, stick } = useStickToBottom();
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => focusInput(inputRef.current), []);

  const ask = (text: string) => {
    stick();
    void send(text);
  };
  const empty = messages.length === 0;

  return (
    <div className="flex h-full flex-col">
      <header className="mx-auto flex h-14 w-full max-w-2xl shrink-0 items-center justify-between px-4">
        <div className="flex items-center gap-2 text-ink">
          <Logo size={24} />
          <span className="font-serif text-lg">RA</span>
        </div>
        <button
          type="button"
          onClick={() => {
            newChat();
            focusInput(inputRef.current);
          }}
          disabled={busy || !ready}
          className="-mr-3 rounded-full px-3 py-1.5 text-sm text-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-muted"
        >
          New chat
        </button>
      </header>

      <main className="min-h-0 flex-1">
        <div
          ref={scroller}
          role="region"
          aria-label="Conversation"
          tabIndex={empty ? undefined : 0}
          className="h-full overflow-x-hidden overflow-y-auto overscroll-contain focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brand/40"
        >
          <div ref={content} className="mx-auto flex min-h-full w-full max-w-2xl flex-col px-4 pt-2 pb-6">
            <h1 className="sr-only">Chat with RA about mutual funds</h1>
            {empty && !booting ? <EmptyState onPick={ask} /> : null}
            <div role="log" aria-live="polite" aria-busy={busy} className="space-y-8">
              {messages.map((m, i) => (
                <Message key={m.id} message={m} onRetry={i === messages.length - 1 ? retry : undefined} />
              ))}
            </div>
          </div>
        </div>
      </main>

      <Composer busy={busy} onSend={ask} inputRef={inputRef} />
    </div>
  );
}

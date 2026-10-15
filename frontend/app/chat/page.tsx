"use client";

import { useEffect, useRef, useState } from "react";
import Markdown from "@/components/Markdown";
import { Logo } from "@/components/Nav";
import RecoCard from "@/components/RecoCard";
import { Button, ErrorNote, RequireInvestor, Spinner } from "@/components/ui";
import { api, streamChat } from "@/lib/api";
import type { RecoCard as Card } from "@/lib/types";

interface Msg {
  id: string;
  role: "user" | "assistant";
  text: string;
  cards: Card[];
  disclosures: string[];
  status?: string;
  streaming?: boolean;
  error?: boolean;
}

let seq = 0;
const nextId = (prefix: string) => `${prefix}-${++seq}`;

const SUGGESTIONS = [
  "How is my portfolio doing?",
  "Explain my RA score. What's the weakest part?",
  "What should I change in my portfolio?",
  "How much tax would I pay if I sold my technology fund?",
  "I want ₹25 lakh for a house in 5 years. How much SIP do I need?",
];

function Disclosures({ keys, texts }: { keys: string[]; texts: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  if (!keys.length) return null;
  return (
    <div className="text-xs text-muted">
      <button onClick={() => setOpen(!open)} className="underline underline-offset-2" aria-expanded={open}>
        {open ? "Hide disclosures" : `Disclosures (${keys.length})`}
      </button>
      {open ? (
        <ul className="mt-2 space-y-1 list-disc pl-4">
          {keys.map((k) => (
            <li key={k}>{texts[k] ?? k}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function ChatView({ investorId }: { investorId: string }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"ra" | "expert">("ra");
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        setTexts(await api<Record<string, string>>("/chat/disclosures"));
        const sessions = await api<{ id: string }[]>(`/investors/${investorId}/chat/sessions`);
        const sid = sessions[0]?.id ?? (await api<{ id: string }>(`/investors/${investorId}/chat/sessions`, { method: "POST" })).id;
        const history = await api<{ id: string; role: "user" | "assistant"; text: string; cards: Card[]; disclosures: string[] }[]>(
          `/chat/sessions/${sid}/messages`,
        );
        if (!live) return;
        setSessionId(sid);
        setMessages(history.map((m) => ({ ...m, text: m.text ?? "" })));
      } catch (e) {
        if (live) setError((e as Error).message);
      }
    })();
    return () => {
      live = false;
    };
  }, [investorId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function newChat() {
    const s = await api<{ id: string }>(`/investors/${investorId}/chat/sessions`, { method: "POST" });
    setSessionId(s.id);
    setMessages([]);
  }

  async function send(text: string) {
    if (!sessionId || !text.trim() || busy) return;
    setBusy(true);
    setInput("");
    setTab("ra");
    const aid = nextId("a");
    setMessages((m) => [
      ...m,
      { id: nextId("u"), role: "user", text, cards: [], disclosures: [] },
      { id: aid, role: "assistant", text: "", cards: [], disclosures: [], status: "Thinking", streaming: true },
    ]);
    const patch = (fn: (m: Msg) => Msg) => setMessages((all) => all.map((m) => (m.id === aid ? fn(m) : m)));
    try {
      for await (const ev of streamChat(sessionId, text)) {
        if (ev.event === "status") patch((m) => ({ ...m, status: ev.data.text }));
        else if (ev.event === "text") patch((m) => ({ ...m, text: m.text + ev.data.delta, status: undefined }));
        else if (ev.event === "cards") patch((m) => ({ ...m, cards: ev.data.cards }));
        else if (ev.event === "done")
          patch((m) => ({ ...m, text: ev.data.text, cards: ev.data.cards ?? m.cards, disclosures: ev.data.disclosures, streaming: false, status: undefined }));
        else if (ev.event === "error") patch((m) => ({ ...m, text: ev.data.message, streaming: false, status: undefined, error: true }));
      }
    } catch (e) {
      patch((m) => ({ ...m, text: (e as Error).message, streaming: false, status: undefined, error: true }));
    } finally {
      patch((m) => ({ ...m, streaming: false, status: undefined }));
      setBusy(false);
    }
  }

  if (error) return <ErrorNote message={error} />;
  if (!sessionId) return <Spinner label="Opening your conversation" />;

  return (
    <div className="max-w-3xl mx-auto flex flex-col min-h-[70vh]">
      <div className="flex items-center gap-2 border-b border-line">
        {(
          [
            ["ra", "RA", "Instant reply"],
            ["expert", "Expert", "Reply within 1 business day"],
          ] as const
        ).map(([key, name, sub]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex-1 py-3 text-center border-b-2 -mb-px ${tab === key ? "border-ink" : "border-transparent text-muted"}`}
          >
            <div className="font-medium">{name}</div>
            <div className="text-xs text-muted">{sub}</div>
          </button>
        ))}
        <Button variant="ghost" className="ml-2 shrink-0" onClick={newChat} disabled={busy}>
          New chat
        </Button>
      </div>

      {tab === "expert" ? (
        <div className="py-10 text-center space-y-3 max-w-md mx-auto">
          <p className="font-serif text-2xl">Your expert always has the last word</p>
          <p className="text-muted text-sm">
            A NISM-certified expert (EUIN holder) reviews your portfolio and RA&apos;s suggestions and replies within one business
            day. It&apos;s free, and you&apos;ll never get inputs nobody stands behind.
          </p>
          <Button onClick={() => send("I'd like a human expert to review my portfolio.")} disabled={busy}>
            Request an expert review
          </Button>
        </div>
      ) : (
        <div className="flex-1 py-6 space-y-6" aria-live="polite">
          {messages.length === 0 ? (
            <div className="text-center py-8 space-y-4">
              <p className="font-serif text-2xl">Ask anything about your funds</p>
              <div className="flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <button key={s} onClick={() => send(s)} className="rounded-full border border-line px-3 py-1.5 text-sm hover:bg-surface-2">
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-2 px-4 py-3 whitespace-pre-wrap">{m.text}</div>
              </div>
            ) : (
              <div key={m.id} className="space-y-3">
                <span className="text-ink">
                  <Logo size={24} />
                </span>
                {m.status && !m.text ? (
                  <p className="text-sm text-muted flex items-center gap-2">
                    <span className="ra-dot h-2 w-2 rounded-full bg-accent" />
                    {m.status}…
                  </p>
                ) : null}
                {m.text ? (
                  <div className={m.error ? "text-bad" : ""}>
                    <Markdown text={m.text} />
                  </div>
                ) : null}
                {m.cards.length ? (
                  <div className="space-y-3">
                    {m.cards.slice(0, 4).map((c, i) => (
                      <RecoCard key={c.id ?? i} card={c} compact />
                    ))}
                  </div>
                ) : null}
                {!m.streaming ? <Disclosures keys={m.disclosures} texts={texts} /> : null}
              </div>
            ),
          )}
          <div ref={bottom} />
        </div>
      )}

      <form
        className="sticky bottom-0 bg-bg pt-2 pb-3"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <div className="flex items-end gap-2 rounded-3xl border border-line bg-surface px-4 py-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            rows={1}
            placeholder="Ask RA, answers in seconds"
            aria-label="Message RA"
            className="flex-1 resize-none bg-transparent py-2 outline-none placeholder:text-muted max-h-40"
          />
          <Button type="submit" disabled={busy || !input.trim()} className="mb-1">
            Send
          </Button>
        </div>
        <p className="text-center text-xs text-muted mt-2">RA is AI and can make mistakes. Your name and PAN are never shared with the AI.</p>
      </form>
    </div>
  );
}

export default function ChatPage() {
  return <RequireInvestor>{(id) => <ChatView investorId={id} />}</RequireInvestor>;
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, streamChat } from "@/lib/api";
import { getInvestorId, useInvestorId } from "@/lib/investor";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** What RA is doing right now ("Comparing the fund with its category"), shown while waiting. */
  status?: string;
  streaming?: boolean;
  /** `text` holds an error message rather than an answer. */
  error?: boolean;
}

interface Opened {
  sessionId: string;
  messages: ChatMessage[];
}

interface HistoryRow {
  id: string;
  role: string;
  text: string | null;
}

let seq = 0;
const nextId = (prefix: string) => `${prefix}-${++seq}`;

// Set-up requests are generous on purpose: aborting a slow POST /investors client-side would
// leave an orphan guest on the server and create another on the retry.
const SETUP_TIMEOUT_MS = 60_000;
const setup = <T>(path: string, init?: RequestInit) => api<T>(path, { ...init, signal: AbortSignal.timeout(SETUP_TIMEOUT_MS) });

// A returning visitor's blank screen gives way to the empty state after this long, even if the
// history is still on its way (it then simply appears above anything they've asked meanwhile).
const BOOT_PATIENCE_MS = 2500;

async function createGuest(): Promise<string> {
  const inv = await setup<{ id: string }>("/investors", { method: "POST", body: JSON.stringify({ display_name: "Guest" }) });
  return inv.id;
}

/** The investor's latest conversation with its messages, or a brand-new one. */
async function openSession(investorId: string, fresh: boolean): Promise<Opened> {
  if (!fresh) {
    const sessions = await setup<{ id: string }[]>(`/investors/${investorId}/chat/sessions`); // newest first
    if (sessions[0]) {
      const rows = await setup<HistoryRow[]>(`/chat/sessions/${sessions[0].id}/messages`);
      const messages = rows
        .filter((r) => r.role === "user" || r.role === "assistant")
        .map((r) => ({ id: r.id, role: r.role as ChatMessage["role"], text: r.text ?? "" }));
      return { sessionId: sessions[0].id, messages };
    }
  }
  const created = await setup<{ id: string }>(`/investors/${investorId}/chat/sessions`, { method: "POST" });
  return { sessionId: created.id, messages: [] };
}

function describe(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof DOMException && (e.name === "TimeoutError" || e.name === "AbortError")) return "RA took too long to respond.";
  if (e instanceof TypeError) return "Can't reach RA right now. Check your connection.";
  return "Something went wrong.";
}

/** State and side effects of the chat window: guest set-up, history, streaming, errors. */
export function useChat() {
  const [investorId, setInvestorId] = useInvestorId();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loaded, setLoaded] = useState(false); // the history question is settled (loaded, empty or failed)
  const [impatient, setImpatient] = useState(false);
  const [busy, setBusy] = useState(false);

  const sessionRef = useRef<string | null>(null);
  const openingRef = useRef<Promise<string> | null>(null); // shared by the mount effect and send()
  const freshRef = useRef(false); // the next session must be new, not the latest one
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  /** Resolves with a usable session id, creating the guest investor and session as needed.
   *  Concurrent callers share one attempt; a failed attempt can simply be retried. */
  const ensureSession = useCallback((): Promise<string> => {
    if (sessionRef.current) return Promise.resolve(sessionRef.current);
    if (!openingRef.current) {
      const fresh = freshRef.current;
      const adopt = async (guest: string) => {
        setLoaded(true); // a brand-new guest has no history to wait for
        setInvestorId(guest);
        return guest;
      };
      openingRef.current = (async () => {
        let investor = getInvestorId();
        let startFresh = fresh;
        if (!investor) {
          investor = await adopt(await createGuest());
          startFresh = true; // a brand-new guest has no conversations to look up
        }
        let opened: Opened;
        try {
          opened = await openSession(investor, startFresh);
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 404)) throw e;
          // The stored investor no longer exists on the server (e.g. a reset database): start over as a new guest.
          investor = await adopt(await createGuest());
          opened = await openSession(investor, true);
        }
        sessionRef.current = opened.sessionId;
        freshRef.current = false;
        setMessages((prev) => [...opened.messages, ...prev]);
        return opened.sessionId;
      })().finally(() => {
        openingRef.current = null;
        setLoaded(true);
      });
    }
    return openingRef.current;
  }, [setInvestorId]);

  const hydrated = investorId !== undefined;
  useEffect(() => {
    // Failures stay silent here; the first send retries and reports them.
    if (hydrated) ensureSession().catch(() => {});
  }, [hydrated, ensureSession]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    const timer = setTimeout(() => setImpatient(true), BOOT_PATIENCE_MS);
    return () => clearTimeout(timer);
  }, []);

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text || busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      const aid = nextId("a");
      setMessages((m) => [
        ...m,
        { id: nextId("u"), role: "user", text },
        { id: aid, role: "assistant", text: "", status: "Thinking", streaming: true },
      ]);
      const patch = (fn: (m: ChatMessage) => ChatMessage) => setMessages((all) => all.map((m) => (m.id === aid ? fn(m) : m)));
      const fail = (message: string) => patch((m) => ({ ...m, text: message, error: true, streaming: false, status: undefined }));

      let finished = false;
      try {
        const sid = await ensureSession();
        for await (const ev of streamChat(sid, text, ctrl.signal)) {
          if (ev.event === "status") patch((m) => ({ ...m, status: ev.data.text }));
          else if (ev.event === "text") patch((m) => ({ ...m, text: m.text + ev.data.delta, status: undefined }));
          else if (ev.event === "done") {
            finished = true;
            // done.text is the compliance-processed answer and replaces whatever was streamed.
            if (ev.data.text.trim()) patch((m) => ({ ...m, text: ev.data.text, streaming: false, status: undefined }));
            else fail("RA didn't have an answer to that.");
          } else if (ev.event === "error") {
            finished = true;
            fail(ev.data.message);
          }
        }
        if (!finished) fail("The connection was interrupted.");
      } catch (e) {
        if (!ctrl.signal.aborted) {
          if (e instanceof ApiError && e.status === 404) {
            // The conversation is gone server-side: the next message starts a new one.
            sessionRef.current = null;
            freshRef.current = true;
          }
          fail(describe(e));
        }
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [ensureSession],
  );

  /** Re-send the last question after an error (drops the failed exchange first). */
  const retry = useCallback(() => {
    const last = messages.at(-1);
    const question = messages.at(-2);
    if (busyRef.current || !last?.error || question?.role !== "user") return;
    setMessages((m) => m.slice(0, -2));
    void send(question.text);
  }, [messages, send]);

  const newChat = useCallback(() => {
    if (busyRef.current) return;
    if (messages.length > 0) {
      sessionRef.current = null; // the next ensureSession() opens a new conversation
      freshRef.current = true;
      setMessages([]);
    }
    ensureSession().catch(() => {});
  }, [messages.length, ensureSession]);

  // Returning visitors see nothing (instead of a flash of the empty state) until their history is in.
  const booting = investorId === undefined || (investorId !== null && !loaded && !impatient);

  return { messages, booting, ready: loaded, busy, send, retry, newChat };
}

"use client";

import { useLayoutEffect, useState, type RefObject } from "react";

/** Focus the input, but only where there is a physical keyboard: on phones it would pop the
 *  on-screen keyboard over the conversation. */
export function focusInput(el: HTMLElement | null) {
  if (el && window.matchMedia("(hover: hover) and (pointer: fine)").matches) el.focus({ preventScroll: true });
}

const MAX_LENGTH = 4000; // the API rejects longer messages

export default function Composer({
  busy,
  onSend,
  inputRef,
}: {
  busy: boolean;
  onSend: (text: string) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const [value, setValue] = useState("");
  const canSend = !busy && value.trim().length > 0;

  // Grow with the text, up to the max height set in CSS (then it scrolls inside).
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value, inputRef]);

  function submit() {
    if (!canSend) return;
    onSend(value);
    setValue("");
    focusInput(inputRef.current);
  }

  return (
    <form
      className="mx-auto w-full max-w-2xl shrink-0 px-4 pt-2 pb-3"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="flex items-end gap-2 rounded-3xl border border-line bg-surface py-1.5 pr-1.5 pl-5 transition-colors focus-within:border-ink/50">
        <label htmlFor="chat-input" className="sr-only">
          Message RA
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends, Shift+Enter adds a line; never send while an IME is composing.
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          rows={1}
          maxLength={MAX_LENGTH}
          enterKeyHint="send"
          placeholder="Ask about any mutual fund…"
          className="max-h-40 min-h-9 flex-1 resize-none bg-transparent py-1.5 text-base leading-6 outline-none placeholder:text-muted"
        />
        <button
          type="submit"
          disabled={!canSend}
          aria-label="Send message"
          className="grid size-9 shrink-0 place-items-center rounded-full bg-brand text-brand-ink transition hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-muted disabled:hover:opacity-100"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 19V5" />
            <path d="m5 12 7-7 7 7" />
          </svg>
        </button>
      </div>
      <p className="mt-2 text-center text-xs text-balance text-muted">Mutual fund investments are subject to market risks. RA is AI and can make mistakes.</p>
    </form>
  );
}

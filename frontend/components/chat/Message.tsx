"use client";

import { memo } from "react";
import Markdown from "@/components/Markdown";
import type { ChatMessage } from "./useChat";

/** One turn of the conversation. Memoised so a streaming reply doesn't re-render earlier ones. */
const Message = memo(function Message({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl bg-surface-2 px-4 py-2.5 leading-6 text-pretty wrap-anywhere whitespace-pre-wrap">{message.text}</p>
      </div>
    );
  }

  return (
    <div className="space-y-3" aria-busy={message.streaming || undefined}>
      {message.text ? (
        message.error ? (
          <div className="space-y-1">
            <p className="text-sm leading-6 text-bad">{message.text}</p>
            {onRetry ? (
              <button
                type="button"
                onClick={onRetry}
                className="rounded-sm text-sm text-muted underline underline-offset-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              >
                Try again
              </button>
            ) : null}
          </div>
        ) : (
          <Markdown text={message.text} />
        )
      ) : null}
      {message.status ? (
        <p className="flex items-center gap-2 text-sm text-muted">
          <span className="ra-dot size-1.5 shrink-0 rounded-full bg-accent" aria-hidden="true" />
          {message.status.replace(/[.…]+$/, "")}…
        </p>
      ) : null}
    </div>
  );
});

export default Message;

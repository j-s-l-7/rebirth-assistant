"use client";

import { useCallback, useEffect, useRef } from "react";

/** Keeps a scroll container pinned to its newest content (streamed text, a growing input),
 *  unless the reader has scrolled up to re-read something. Call `stick()` after sending. */
export function useStickToBottom() {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const box = scroller.current;
    const inner = content.current;
    if (!box || !inner) return;
    const onScroll = () => {
      pinned.current = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    };
    const follow = () => {
      if (pinned.current) box.scrollTop = box.scrollHeight;
    };
    // Fires when the content grows (new text) or the box shrinks (taller input, on-screen keyboard).
    const observer = new ResizeObserver(follow);
    observer.observe(box);
    observer.observe(inner);
    box.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      observer.disconnect();
      box.removeEventListener("scroll", onScroll);
    };
  }, []);

  const stick = useCallback(() => {
    pinned.current = true;
  }, []);

  return { scroller, content, stick };
}

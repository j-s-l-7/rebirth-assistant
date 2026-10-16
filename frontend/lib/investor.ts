"use client";

import { useCallback, useSyncExternalStore } from "react";

// Until sign-in ships, each browser chats as its own guest; the id lives in this browser only.
const KEY = "ra.investorId";
const EVENT = "ra-investor-change";

function read(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function subscribe(cb: () => void) {
  window.addEventListener(EVENT, cb);
  window.addEventListener("storage", cb);
  return () => {
    window.removeEventListener(EVENT, cb);
    window.removeEventListener("storage", cb);
  };
}

export function setInvestorId(id: string | null) {
  try {
    if (id) window.localStorage.setItem(KEY, id);
    else window.localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: selection lasts for this page only */
  }
  window.dispatchEvent(new Event(EVENT));
}

/** The selected investor right now (client only), for code that runs outside React renders. */
export function getInvestorId(): string | null {
  return read();
}

/** Returns undefined during server render / before hydration, then the id or null. */
export function useInvestorId(): [string | null | undefined, (id: string | null) => void] {
  const id = useSyncExternalStore(subscribe, read, () => undefined);
  const set = useCallback((v: string | null) => setInvestorId(v), []);
  return [id, set];
}

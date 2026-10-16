"use client";

import { useCallback, useSyncExternalStore } from "react";

// Until OTP sign-in ships (phase 2) the selected investor lives in this browser only.
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

/** Shareable demo links: any page accepts ?investor=<id>. Remember it, drop it from the URL
 *  and return it (null when the link carries none). Client only. */
export function adoptInvestorFromLink(): string | null {
  const url = new URL(window.location.href);
  const fromLink = url.searchParams.get("investor");
  if (!fromLink) return null;
  setInvestorId(fromLink);
  url.searchParams.delete("investor");
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  return fromLink;
}

/** Returns undefined during server render / before hydration, then the id or null. */
export function useInvestorId(): [string | null | undefined, (id: string | null) => void] {
  const id = useSyncExternalStore(subscribe, read, () => undefined);
  const set = useCallback((v: string | null) => setInvestorId(v), []);
  return [id, set];
}

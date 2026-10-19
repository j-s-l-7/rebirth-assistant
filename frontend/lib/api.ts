export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const body = await res.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(message || `Request failed (${res.status})`, res.status);
  }
  return res.json() as Promise<T>;
}

export type ChatEvent =
  | { event: "status"; data: { text: string; escalation_id?: string } }
  | { event: "text"; data: { delta: string } }
  | { event: "done"; data: { text: string; disclosures: string[]; escalation_id?: string | null } }
  | { event: "error"; data: { message: string } };

/** POST a chat message and yield server-sent events as they arrive. */
export async function* streamChat(sessionId: string, text: string, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${API_BASE}/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
  });
  if (!res.ok || !res.body) throw new ApiError(`Chat failed (${res.status})`, res.status);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) yield { event, data: JSON.parse(data) } as ChatEvent;
    }
  }
}

import { ChatEvent } from "./chat-types";

// Browsers' native EventSource can't send a POST body, and POST /api/chat/message needs one
// (session_id/role/message) — this is the standard workaround: a plain fetch with a streamed
// response body, hand-parsed for "data: <json>\n\n" frames. One frame per ChatEvent (see
// sentinel/chat/sse.py's format_event) — no named SSE event types to split on, just the `type`
// field inside the JSON payload.
export async function* streamChatEvents(
  url: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    yield { type: "error", message: `sentinel-chat returned ${res.status}` };
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Frames are separated by a blank line; a frame may arrive split across chunks, so only
      // consume complete frames and keep any trailing partial frame in the buffer.
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (line) {
          try {
            yield JSON.parse(line.slice("data: ".length)) as ChatEvent;
          } catch {
            // malformed frame — skip rather than abort the whole stream over one bad frame
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

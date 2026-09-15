import { NextRequest, NextResponse } from "next/server";

// Server-side proxy to sentinel/chat/api.py, same shape as the monitor/incidents passthroughs in
// ../monitor and ../incidents. The chat mediator sets no CORS headers of its own — it wasn't
// built to be fetched from a browser origin directly — so this same-origin route is what lets
// the browser reach it without a CORS error.
//
// POST /chat/message is the one streaming path (SSE); everything else (POST /chat/resume,
// GET /chat/sessions[/...]) is a single buffered JSON response — same "detect by path, not by
// method" convention the monitor proxy uses for its /stream endpoint.
const CHAT_API_URL = process.env.CHAT_API_URL ?? "http://localhost:8093";

export const dynamic = "force-dynamic";

async function proxy(req: NextRequest, path: string[]) {
  const search = req.nextUrl.search;
  // sentinel/chat/api.py's own routes are /chat/message, /chat/sessions, etc. — the "chat"
  // segment is part of its route path, not just this proxy's URL prefix, so it has to be added
  // back in rather than stripped (unlike monitor/incidents, whose backends have no such prefix).
  const url = `${CHAT_API_URL}/chat/${path.join("/")}${search}`;
  const isStream = path[path.length - 1] === "message";

  const init: RequestInit & { duplex?: "half" } = {
    method: req.method,
    headers: { "Content-Type": "application/json" },
    // Forwards the browser's own abort (the user hit stop, or navigated away) into this proxy's
    // fetch to sentinel-chat, so aborting client-side actually tears down the upstream connection
    // instead of leaving `graph.astream` running server-side with nobody reading its output —
    // `request.is_disconnected()` in `sentinel/chat/api.py` observes exactly this signal.
    signal: req.signal,
  };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
    // Node's fetch requires this for a streaming request body; harmless for the non-streaming
    // POSTs here too, and documents that this passthrough can carry a stream.
    init.duplex = "half";
  }

  try {
    const res = await fetch(url, init);

    if (isStream) {
      return new NextResponse(res.body, {
        status: res.status,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("Content-Type") ?? "application/json" },
    });
  } catch {
    // sentinel-chat is a local dev service, not always running — surface "unreachable" as
    // structured JSON so the UI can render a clear offline state instead of a fetch throw.
    return NextResponse.json({ error: "sentinel-chat unreachable" }, { status: 502 });
  }
}

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ _path: string[] }> },
) {
  const { _path } = await params;
  return proxy(req, _path);
}

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ _path: string[] }> },
) {
  const { _path } = await params;
  return proxy(req, _path);
}

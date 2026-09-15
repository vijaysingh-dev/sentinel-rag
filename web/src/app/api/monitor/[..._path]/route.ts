import { NextRequest, NextResponse } from "next/server";

// Server-side proxy to system-monitor (app/system-monitor), same shape as the LangGraph
// passthrough in ../../[..._path]/route.ts. system-monitor has no CORS headers of its own —
// it was built to be polled by the RCA agent's own backend, not fetched from a browser origin
// — so a same-origin Next.js route is what lets the chat UI read it without touching the
// Python service.
const MONITOR_API_URL = process.env.MONITOR_API_URL ?? "http://localhost:8091";

// SSE passthrough must not be cached or statically evaluated — each request is a live stream.
export const dynamic = "force-dynamic";

async function proxy(req: NextRequest, path: string[]) {
  const search = req.nextUrl.search;
  const url = `${MONITOR_API_URL}/${path.join("/")}${search}`;
  const isStream = path[path.length - 1] === "stream";

  try {
    const res = await fetch(url, {
      headers: { Accept: isStream ? "text/event-stream" : "application/json" },
      // @ts-expect-error -- Node's fetch requires this for a duplex request body; not needed
      // here (GET has no body) but harmless, and documents that this passthrough is streaming.
      duplex: "half",
    });
    // SSE must be forwarded as a live stream, not buffered with res.text() — an incident or
    // health change needs to reach the browser as it happens, not after the connection ends.
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
    // system-monitor is a local dev sidecar, not always running — surface "unreachable" as
    // structured JSON so the panel can render a clear offline state instead of a fetch throw.
    return NextResponse.json({ error: "system-monitor unreachable" }, { status: 502 });
  }
}

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ _path: string[] }> },
) {
  const { _path } = await params;
  return proxy(req, _path);
}

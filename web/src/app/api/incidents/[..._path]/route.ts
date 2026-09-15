import { NextRequest, NextResponse } from "next/server";

// Server-side proxy to incident-generator (app/incident-generator) — same reasoning as
// ../monitor/[..._path]/route.ts. POST is needed here (unlike monitor) for the manual
// /trigger/{name} override. No /stream to special-case here: incident-generator's state only
// ever changes in response to a trigger this same panel made, so the platform panel just
// fetches /status directly instead of subscribing to a push channel.
const INCIDENT_API_URL = process.env.INCIDENT_API_URL ?? "http://localhost:8092";

async function proxy(req: NextRequest, path: string[]) {
  const search = req.nextUrl.search;
  const url = `${INCIDENT_API_URL}/${path.join("/")}${search}`;

  try {
    const res = await fetch(url, {
      method: req.method,
      headers: { Accept: "application/json" },
    });
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("Content-Type") ?? "application/json" },
    });
  } catch {
    return NextResponse.json({ error: "incident-generator unreachable" }, { status: 502 });
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

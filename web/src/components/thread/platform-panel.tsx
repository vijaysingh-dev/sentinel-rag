"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "../ui/button";
import { ChevronDown, ChevronRight, Wifi, WifiOff, Zap } from "lucide-react";

type HealthStatus = "healthy" | "starting" | "unhealthy" | "missing";

interface TreeNode {
  name: string;
  display_name: string;
  own: HealthStatus;
  status: HealthStatus;
  children: TreeNode[];
}

interface TreeResponse {
  computed_at: number;
  trees: TreeNode[];
}

interface IncidentRecord {
  ts: number;
  event: "incident_start" | "incident_end";
  scenario: string;
  flag: string;
  fault_variant: string;
  note: string;
}

interface RecentIncident {
  scenario: string;
  flag: string;
  fault_variant: string;
  note: string;
  start_ts: number;
  end_ts: number | null;
}

interface IncidentStatus {
  active: IncidentRecord | null;
  // Already paired start/end by the backend (generator.py's snapshot()) — each entry is one run.
  recent: RecentIncident[];
}

interface Scenario {
  name: string;
  flag: string;
  fault_variant: string;
  reset_variant: string;
  duration_s: number;
  note: string;
}

const STATUS_STYLES: Record<HealthStatus, string> = {
  healthy: "bg-green-500",
  starting: "bg-yellow-400",
  unhealthy: "bg-red-500",
  missing: "bg-gray-300",
};

/**
 * Subscribes to a service's `/stream` SSE endpoint and keeps `data` in sync. The browser's own
 * EventSource retries on a drop, but with a fixed, immediate backoff; this layers exponential
 * backoff (capped at 15s) on top so a sidecar that's down for a while (e.g. `make demo-down`)
 * doesn't get hammered with reconnect attempts every second.
 */
function useEventStream<T>(url: string, eventName: string) {
  const [data, setData] = useState<T | null>(null);
  const [connected, setConnected] = useState(false);
  const retryDelay = useRef(1000);

  useEffect(() => {
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      es = new EventSource(url);

      es.addEventListener(eventName, (evt) => {
        retryDelay.current = 1000; // a good frame means the connection is healthy again
        setConnected(true);
        try {
          setData(JSON.parse((evt as MessageEvent).data));
        } catch {
          // malformed frame — ignore, keep the last good data rather than blanking the panel
        }
      });
      es.addEventListener("heartbeat", () => setConnected(true));

      es.onerror = () => {
        setConnected(false);
        es?.close();
        retryTimer = setTimeout(connect, retryDelay.current);
        retryDelay.current = Math.min(retryDelay.current * 2, 15_000);
      };
    };

    connect();
    return () => {
      cancelled = true;
      es?.close();
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [url, eventName]);

  return { data, connected };
}

function ConnectionIndicator({ connected }: { connected: boolean }) {
  return connected ? (
    <Wifi
      className="size-3.5 text-green-600"
      aria-label="live"
    />
  ) : (
    <WifiOff
      className="size-3.5 text-gray-400"
      aria-label="disconnected"
    />
  );
}

function SectionHeader({
  title,
  collapsed,
  onToggle,
  right,
}: {
  title: string;
  collapsed: boolean;
  onToggle: () => void;
  right?: React.ReactNode;
}) {
  return (
    <button
      onClick={onToggle}
      className="mb-2 flex w-full cursor-pointer items-center justify-between gap-2 text-left"
      aria-expanded={!collapsed}
    >
      <span className="flex min-w-0 items-center gap-1">
        {collapsed ? (
          <ChevronRight className="size-3.5 shrink-0 text-gray-400" />
        ) : (
          <ChevronDown className="size-3.5 shrink-0 text-gray-400" />
        )}
        <h3 className="truncate text-sm font-semibold">{title}</h3>
      </span>
      {right && <span className="shrink-0">{right}</span>}
    </button>
  );
}

function HealthTreeRow({
  node,
  depth,
  collapsed,
  toggle,
}: {
  node: TreeNode;
  depth: number;
  collapsed: Set<string>;
  toggle: (name: string) => void;
}) {
  const hasChildren = node.children.length > 0;
  const isCollapsed = collapsed.has(node.name);

  return (
    <div>
      <div
        className="flex items-center gap-1 py-1 text-sm"
        style={{ paddingLeft: depth * 16 }}
      >
        {hasChildren ? (
          <button
            onClick={() => toggle(node.name)}
            className="flex size-4 shrink-0 cursor-pointer items-center justify-center text-gray-400 hover:text-gray-700"
            aria-label={isCollapsed ? `Expand ${node.display_name}` : `Collapse ${node.display_name}`}
          >
            {isCollapsed ? (
              <ChevronRight className="size-3.5" />
            ) : (
              <ChevronDown className="size-3.5" />
            )}
          </button>
        ) : (
          <span className="size-4 shrink-0" />
        )}
        <span
          className={cn(
            "inline-block size-2.5 shrink-0 rounded-full",
            STATUS_STYLES[node.status],
          )}
          title={node.status}
        />
        <span className={cn(node.status === "unhealthy" && "font-medium text-red-700")}>
          {node.display_name}
        </span>
        {node.own !== node.status && (
          <span className="text-xs text-gray-400">(own: {node.own})</span>
        )}
        {hasChildren && isCollapsed && (
          <span className="text-xs text-gray-400">({node.children.length})</span>
        )}
      </div>
      {hasChildren &&
        !isCollapsed &&
        node.children.map((child) => (
          <HealthTreeRow
            key={child.name}
            node={child}
            depth={depth + 1}
            collapsed={collapsed}
            toggle={toggle}
          />
        ))}
    </div>
  );
}

function SystemMonitorSection() {
  const { data: tree, connected } = useEventStream<TreeResponse>("/api/monitor/stream", "tree");
  const [collapsedNodes, setCollapsedNodes] = useState<Set<string>>(new Set());
  const [sectionCollapsed, setSectionCollapsed] = useState(false);

  const toggle = (name: string) => {
    setCollapsedNodes((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="border-b p-4">
      <SectionHeader
        title="System health"
        collapsed={sectionCollapsed}
        onToggle={() => setSectionCollapsed((c) => !c)}
        right={<ConnectionIndicator connected={connected} />}
      />
      {!sectionCollapsed && (
        <>
          {!connected && !tree && (
            <p className="text-xs text-gray-400">
              Connecting to system-monitor — is <code>make demo-up</code> running?
            </p>
          )}
          {tree?.trees.map((root) => (
            <HealthTreeRow
              key={root.name}
              node={root}
              depth={0}
              collapsed={collapsedNodes}
              toggle={toggle}
            />
          ))}
        </>
      )}
    </div>
  );
}

function formatAgo(ts: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function formatClock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(startTs: number, endTs: number): string {
  const s = Math.max(0, Math.round(endTs - startTs));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function IncidentGeneratorSection() {
  const [status, setStatus] = useState<IncidentStatus | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [triggering, setTriggering] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);
  const [sectionCollapsed, setSectionCollapsed] = useState(false);
  const fetchedOnce = useRef(false);

  const refreshStatus = () => {
    fetch("/api/incidents/status")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        setOffline(data === null);
        if (data) setStatus(data);
      })
      .catch(() => setOffline(true));
  };

  useEffect(() => {
    // incident-generator has no background loop pushing state on its own — it only ever changes
    // in response to a trigger this panel itself makes — so there's nothing for SSE to buy here.
    // A snapshot on first open, refreshed after each trigger, is the whole story.
    if (fetchedOnce.current) return;
    fetchedOnce.current = true;
    refreshStatus();
    fetch("/api/incidents/scenarios")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) setScenarios(data.scenarios);
      })
      .catch(() => {});
  }, []);

  const trigger = async (name: string) => {
    setTriggering(name);
    try {
      await fetch(`/api/incidents/trigger/${name}`, { method: "POST" });
    } finally {
      setTriggering(null);
      refreshStatus();
    }
  };

  return (
    <div className="p-4">
      <SectionHeader
        title="Incident generator"
        collapsed={sectionCollapsed}
        onToggle={() => setSectionCollapsed((c) => !c)}
      />
      {!sectionCollapsed && (
        <>
          {offline && !status && (
            <p className="text-xs text-gray-400">
              incident-generator unreachable — is <code>make demo-up</code> running?
            </p>
          )}

          {status && (
            <>
              <div className="mb-3 rounded-md border p-2.5 text-sm">
                {status.active ? (
                  <div className="flex items-center gap-2">
                    <span className="inline-block size-2.5 shrink-0 animate-pulse rounded-full bg-red-500" />
                    <div>
                      <div className="font-medium">{status.active.scenario}</div>
                      <div className="text-xs text-gray-500">
                        {status.active.flag} = {status.active.fault_variant} · started{" "}
                        {formatAgo(status.active.ts)}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-gray-500">
                    <span className="inline-block size-2.5 shrink-0 rounded-full bg-green-500" />
                    No active incident
                  </div>
                )}
              </div>

              <div className="mb-4">
                <div className="mb-1.5 text-xs font-medium text-gray-500 uppercase">
                  Trigger scenario
                </div>
                <div className="flex flex-col gap-1">
                  {scenarios.map((s) => (
                    <Button
                      key={s.name}
                      variant="outline"
                      size="sm"
                      className="h-auto justify-start gap-2 px-2 py-1.5 text-xs"
                      disabled={!!status.active || triggering !== null}
                      onClick={() => trigger(s.name)}
                      title={s.note}
                    >
                      <Zap className="size-3 shrink-0" />
                      <span className="truncate">
                        {triggering === s.name ? "triggering…" : s.name}
                      </span>
                    </Button>
                  ))}
                </div>
              </div>

              <div>
                <div className="mb-1.5 text-xs font-medium text-gray-500 uppercase">
                  Recent incidents
                </div>
                <div className="flex flex-col gap-1">
                  {status.recent.length === 0 && (
                    <p className="text-xs text-gray-400">None yet.</p>
                  )}
                  {[...status.recent].reverse().map((r, i) => (
                    <div
                      key={`${r.scenario}-${r.start_ts}-${i}`}
                      className="text-xs text-gray-600"
                    >
                      <span className="text-gray-400">{formatAgo(r.end_ts ?? r.start_ts)}</span>{" "}
                      {r.scenario} · {formatClock(r.start_ts)}
                      {r.end_ts != null && `–${formatClock(r.end_ts)}`}
                      {r.end_ts != null && (
                        <span className="text-gray-400"> ({formatDuration(r.start_ts, r.end_ts)})</span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

export function PlatformPanel() {
  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <SystemMonitorSection />
      <IncidentGeneratorSection />
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, LoaderCircle, Copy, Check, RotateCcw } from "lucide-react";
import { DisplayMessage } from "@/providers/Stream";
import { useStreamContext } from "@/providers/Stream";
import { MarkdownText } from "../markdown-text";
import { useCopyToClipboard } from "../use-copy-to-clipboard";
import { cn } from "@/lib/utils";
import { Route } from "@/lib/chat-types";
import { ThinkingStep } from "@/lib/chat-types";

const ROUTE_LABELS: Record<Route, string> = {
  tool_call: "Status check",
  rag: "Documentation",
  rca: "Investigation",
  refuse: "Out of scope",
};

const ROUTE_STYLES: Record<Route, string> = {
  tool_call: "bg-blue-50 text-blue-700",
  rag: "bg-emerald-50 text-emerald-700",
  rca: "bg-amber-50 text-amber-700",
  refuse: "bg-gray-100 text-gray-500",
};

function RouteBadge({ route }: { route: Route }) {
  return (
    <span
      className={cn(
        "mb-1 inline-block w-fit rounded-full px-2 py-0.5 text-xs font-medium",
        ROUTE_STYLES[route],
      )}
    >
      {ROUTE_LABELS[route]}
    </span>
  );
}

function Refs({ refs }: { refs: string[] }) {
  if (!refs.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {refs.map((ref) => (
        <span
          key={ref}
          className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-500"
          title={ref}
        >
          {ref.split("::")[0]}
        </span>
      ))}
    </div>
  );
}

// Collapsed by default once the answer has landed (content present), expanded automatically
// while still investigating (no content yet) — the same "auto-open while working, collapse once
// answered" behavior as ChatGPT's reasoning drawer, so the user isn't stuck expanding it mid-run
// to see why the wait is long.
function ThinkingSteps({ steps, live }: { steps: ThinkingStep[]; live: boolean }) {
  const [open, setOpen] = useState(live);
  const userToggled = useRef(false);
  useEffect(() => {
    if (!live && !userToggled.current) setOpen(false);
  }, [live]);
  if (!steps.length) return null;
  return (
    <div className="mb-1 w-full max-w-xl rounded-lg border bg-gray-50/80 text-sm">
      <button
        type="button"
        onClick={() => {
          userToggled.current = true;
          setOpen((p) => !p);
        }}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-gray-600"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        {live && <LoaderCircle className="size-3.5 animate-spin text-gray-400" />}
        <span className="font-medium">
          {live ? "Investigating" : `Investigated in ${steps.length} step${steps.length === 1 ? "" : "s"}`}
        </span>
      </button>
      {/* max-h caps growth at ~10 step rows before scrolling kicks in; below that the list's
          height simply follows however many steps have streamed in so far. */}
      {open && (
        <ul className="flex max-h-64 flex-col gap-1 overflow-y-auto border-t px-2.5 py-2">
          {steps.map((step, i) => (
            <li
              key={`${step.node}-${i}`}
              className="flex items-baseline gap-2 text-xs text-gray-500"
            >
              <span className="font-mono text-gray-400">{step.node}</span>
              <span>{step.summary}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function AssistantMessage({
  message,
  isLatest,
}: {
  message: DisplayMessage;
  isLatest: boolean;
}) {
  const { isLoading, retryMessage } = useStreamContext();
  const { copied, copy } = useCopyToClipboard();
  const refs = message.meta?.refs as string[] | undefined;
  const steps = message.steps ?? [];
  const live = steps.length > 0 && message.content === "";
  return (
    <div className="group flex flex-col gap-1">
      {message.route && <RouteBadge route={message.route} />}
      <ThinkingSteps steps={steps} live={live} />
      <div className="py-1">
        <MarkdownText>{message.content}</MarkdownText>
      </div>
      {refs && <Refs refs={refs} />}
      {!live && message.content && (
        <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
          <button
            type="button"
            aria-label="Copy response"
            onClick={() => copy(message.content)}
            className="text-muted-foreground hover:text-foreground p-1"
          >
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          </button>
          {isLatest && !isLoading && (
            <button
              type="button"
              aria-label="Retry response"
              onClick={() => retryMessage(message.id)}
              className="text-muted-foreground hover:text-foreground p-1"
            >
              <RotateCcw className="size-3.5" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function AssistantMessageLoading() {
  return (
    <div className="flex items-center gap-2 py-1">
      <div className="flex gap-1">
        <span className="size-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:-0.3s]" />
        <span className="size-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:-0.15s]" />
        <span className="size-1.5 animate-bounce rounded-full bg-gray-400" />
      </div>
    </div>
  );
}

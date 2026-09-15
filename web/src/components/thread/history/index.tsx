import { Button } from "@/components/ui/button";
import { useThreads } from "@/providers/Thread";
import { useStreamContext } from "@/providers/Stream";
import { ChatSession } from "@/lib/chat-types";
import { useEffect } from "react";

import { useQueryState, parseAsBoolean } from "nuqs";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { PanelRightOpen, PanelRightClose } from "lucide-react";
import { useMediaQuery } from "@/hooks/useMediaQuery";

function SessionList({
  sessions,
  onSessionClick,
}: {
  sessions: ChatSession[];
  onSessionClick?: () => void;
}) {
  const { sessionId, loadSession } = useStreamContext();

  return (
    <div className="flex h-full w-full flex-col items-start justify-start gap-2 overflow-y-scroll [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent">
      {sessions.map((s) => (
        <div
          key={s.session_id}
          className="w-full px-1"
        >
          <Button
            variant="ghost"
            className="w-full items-start justify-start text-left font-normal"
            onClick={(e) => {
              e.preventDefault();
              onSessionClick?.();
              if (s.session_id === sessionId) return;
              loadSession(s.session_id);
            }}
          >
            <p className="truncate text-ellipsis">{s.title || s.session_id}</p>
          </Button>
        </div>
      ))}
    </div>
  );
}

function ThreadHistoryLoading() {
  return (
    <div className="flex h-full w-full flex-col items-start justify-start gap-2 overflow-y-scroll [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent">
      {Array.from({ length: 30 }).map((_, i) => (
        <Skeleton
          key={`skeleton-${i}`}
          className="h-10 w-full"
        />
      ))}
    </div>
  );
}

export default function ThreadHistory() {
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");
  const [chatHistoryOpen, setChatHistoryOpen] = useQueryState(
    "chatHistoryOpen",
    parseAsBoolean.withDefault(false),
  );

  const {
    getSessions,
    sessions,
    setSessions,
    sessionsLoading,
    setSessionsLoading,
  } = useThreads();

  useEffect(() => {
    if (typeof window === "undefined") return;
    setSessionsLoading(true);
    getSessions()
      .then(setSessions)
      .catch(console.error)
      .finally(() => setSessionsLoading(false));
  }, []);

  return (
    <>
      <div className="shadow-inner-right hidden h-screen w-full shrink-0 flex-col items-start justify-start gap-6 border-r-[1px] border-slate-300 lg:flex">
        <div className="flex w-full min-w-0 items-center justify-between gap-2 px-4 pt-1.5">
          <Button
            className="shrink-0 hover:bg-gray-100"
            variant="ghost"
            onClick={() => setChatHistoryOpen((p) => !p)}
          >
            {chatHistoryOpen ? (
              <PanelRightOpen className="size-5" />
            ) : (
              <PanelRightClose className="size-5" />
            )}
          </Button>
          <h1 className="truncate font-semibold">Conversations</h1>
        </div>
        {sessionsLoading ? (
          <ThreadHistoryLoading />
        ) : (
          <SessionList sessions={sessions} />
        )}
      </div>
      <div className="lg:hidden">
        <Sheet
          open={!!chatHistoryOpen && !isLargeScreen}
          onOpenChange={(open) => {
            if (isLargeScreen) return;
            setChatHistoryOpen(open);
          }}
        >
          <SheetContent
            side="left"
            className="flex lg:hidden"
          >
            <SheetHeader>
              <SheetTitle>Conversations</SheetTitle>
            </SheetHeader>
            <SessionList
              sessions={sessions}
              onSessionClick={() => setChatHistoryOpen((o) => !o)}
            />
          </SheetContent>
        </Sheet>
      </div>
    </>
  );
}

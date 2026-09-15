import { ReactNode, FormEvent, useState } from "react";
import { cn } from "@/lib/utils";
import { useStreamContext } from "@/providers/Stream";
import { Button } from "../ui/button";
import { AssistantMessage, AssistantMessageLoading } from "./messages/ai";
import { HumanMessage } from "./messages/human";
import { TooltipIconButton } from "./tooltip-icon-button";
import {
  ArrowDown,
  PanelRightOpen,
  PanelRightClose,
  SquarePen,
  Square,
  XIcon,
  Activity,
} from "lucide-react";
import { useQueryState, parseAsBoolean } from "nuqs";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";
import ThreadHistory from "./history";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { PlatformPanel } from "./platform-panel";
import { RoleSelect } from "./role-select";
import { ApprovalPanel } from "./approval-panel";

const THREAD_HISTORY_WIDTH = 240;

function StickyToBottomContent(props: {
  content: ReactNode;
  footer?: ReactNode;
  className?: string;
  contentClassName?: string;
}) {
  const context = useStickToBottomContext();
  return (
    <div
      ref={context.scrollRef}
      style={{ width: "100%", height: "100%" }}
      className={props.className}
    >
      <div
        ref={context.contentRef}
        className={props.contentClassName}
      >
        {props.content}
      </div>
      {props.footer}
    </div>
  );
}

function ScrollToBottom(props: { className?: string }) {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();
  if (isAtBottom) return null;
  return (
    <Button
      variant="outline"
      className={props.className}
      onClick={() => scrollToBottom()}
    >
      <ArrowDown className="h-4 w-4" />
      <span>Scroll to bottom</span>
    </Button>
  );
}

export function Thread() {
  const [chatHistoryOpen, setChatHistoryOpen] = useQueryState(
    "chatHistoryOpen",
    parseAsBoolean.withDefault(false),
  );
  const [platformPanelOpen, setPlatformPanelOpen] = useQueryState(
    "platformPanelOpen",
    parseAsBoolean.withDefault(false),
  );
  const [input, setInput] = useState("");
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");

  const stream = useStreamContext();
  const { messages, isLoading, sessionId, newSession, pendingApproval, stop } = stream;

  const chatStarted = !!sessionId || !!messages.length;
  const lastAssistantId = [...messages].reverse().find((m) => m.turn_role === "assistant")?.id;

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim().length === 0 || isLoading) return;
    const text = input;
    setInput("");
    stream.sendMessage(text);
  };

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <div className="relative hidden lg:flex">
        <div
          className="absolute z-20 h-full overflow-hidden border-r bg-white transition-transform duration-300"
          style={{
            width: THREAD_HISTORY_WIDTH,
            transform: chatHistoryOpen ? "translateX(0)" : `translateX(-${THREAD_HISTORY_WIDTH}px)`,
          }}
        >
          <div
            className="relative h-full"
            style={{ width: THREAD_HISTORY_WIDTH }}
          >
            <ThreadHistory />
          </div>
        </div>
      </div>

      <div
        className={cn(
          "grid w-full grid-cols-[1fr_0fr] transition-all duration-500",
          platformPanelOpen && "grid-cols-[75fr_25fr]",
        )}
      >
        <div
          className="relative flex min-w-0 flex-1 flex-col overflow-hidden transition-all duration-300"
          style={{
            marginLeft: chatHistoryOpen && isLargeScreen ? THREAD_HISTORY_WIDTH : 0,
          }}
        >
          <div className="relative z-10 flex items-center justify-between gap-3 p-2">
            <div className="relative flex items-center justify-start gap-2">
              {(!chatHistoryOpen || !isLargeScreen) && (
                <Button
                  className="hover:bg-gray-100"
                  variant="ghost"
                  onClick={() => setChatHistoryOpen((p) => !p)}
                >
                  {chatHistoryOpen ? (
                    <PanelRightOpen className="size-5" />
                  ) : (
                    <PanelRightClose className="size-5" />
                  )}
                </Button>
              )}
              <button
                className="flex cursor-pointer items-center gap-2"
                onClick={newSession}
              >
                <span className="text-xl font-semibold tracking-tight">Sentinel</span>
              </button>
            </div>

            <div className="flex items-center gap-3">
              <RoleSelect disabled={chatStarted} />
              {!platformPanelOpen && (
                <TooltipIconButton
                  tooltip="Platform status"
                  variant="ghost"
                  onClick={() => setPlatformPanelOpen((p) => !p)}
                >
                  <Activity className="size-5" />
                </TooltipIconButton>
              )}
              {chatStarted && (
                <TooltipIconButton
                  size="lg"
                  className="p-4"
                  tooltip="New conversation"
                  variant="ghost"
                  onClick={newSession}
                >
                  <SquarePen className="size-5" />
                </TooltipIconButton>
              )}
            </div>

            <div className="from-background to-background/0 absolute inset-x-0 top-full h-5 bg-gradient-to-b" />
          </div>

          <StickToBottom className="relative flex-1 overflow-hidden">
            <StickyToBottomContent
              className={cn(
                "absolute inset-0 overflow-y-scroll px-4 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent",
                !chatStarted && "mt-[25vh] flex flex-col items-stretch",
                chatStarted && "grid grid-rows-[1fr_auto]",
              )}
              contentClassName="pt-8 pb-16 max-w-3xl mx-auto flex flex-col gap-4 w-full"
              content={
                <>
                  {messages.map((message) =>
                    message.turn_role === "user" ? (
                      <HumanMessage
                        key={message.id}
                        message={message}
                      />
                    ) : (
                      <AssistantMessage
                        key={message.id}
                        message={message}
                        isLatest={message.id === lastAssistantId}
                      />
                    ),
                  )}
                  {isLoading &&
                    messages[messages.length - 1]?.content === "" &&
                    !messages[messages.length - 1]?.steps?.length && <AssistantMessageLoading />}
                  {pendingApproval && <ApprovalPanel />}
                </>
              }
              footer={
                <div className="sticky bottom-0 flex flex-col items-center gap-8 bg-white">
                  {!chatStarted && (
                    <div className="flex flex-col items-center gap-2 text-center">
                      <h1 className="text-2xl font-semibold tracking-tight">Sentinel</h1>
                      <p className="text-muted-foreground max-w-md text-sm">
                        Ask about the OpenTelemetry Demo platform — service health, incidents,
                        runbooks, or why something is failing right now.
                      </p>
                    </div>
                  )}

                  <ScrollToBottom className="animate-in fade-in-0 zoom-in-95 absolute bottom-full left-1/2 mb-4 -translate-x-1/2" />

                  <div className="bg-muted relative z-10 mx-auto mb-8 w-full max-w-3xl rounded-2xl border border-solid shadow-xs transition-all">
                    <form
                      onSubmit={handleSubmit}
                      className="mx-auto grid max-w-3xl grid-rows-[1fr_auto] gap-2"
                    >
                      <textarea
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={(e) => {
                          if (
                            e.key === "Enter" &&
                            !e.shiftKey &&
                            !e.metaKey &&
                            !e.nativeEvent.isComposing
                          ) {
                            e.preventDefault();
                            const el = e.target as HTMLElement | undefined;
                            const form = el?.closest("form");
                            form?.requestSubmit();
                          }
                        }}
                        placeholder="Ask about the platform..."
                        className="field-sizing-content resize-none border-none bg-transparent p-3.5 pb-0 shadow-none ring-0 outline-none focus:ring-0 focus:outline-none"
                      />

                      <div className="flex items-center gap-6 p-2 pt-4">
                        {isLoading ? (
                          <Button
                            key="stop"
                            type="button"
                            onClick={stop}
                            className="ml-auto"
                          >
                            <Square className="h-3.5 w-3.5 fill-current" />
                            Stop
                          </Button>
                        ) : (
                          <Button
                            type="submit"
                            className="ml-auto shadow-md transition-all"
                            disabled={!input.trim()}
                          >
                            Send
                          </Button>
                        )}
                      </div>
                    </form>
                  </div>
                </div>
              }
            />
          </StickToBottom>
        </div>

        <div className="relative flex flex-col border-l">
          <div className="absolute inset-0 flex flex-col">
            {platformPanelOpen && (
              <>
                <div className="grid grid-cols-[1fr_auto] border-b p-4">
                  <span className="truncate overflow-hidden font-semibold">Platform status</span>
                  <button
                    onClick={() => setPlatformPanelOpen(false)}
                    className="cursor-pointer"
                  >
                    <XIcon className="size-5" />
                  </button>
                </div>
                <PlatformPanel />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

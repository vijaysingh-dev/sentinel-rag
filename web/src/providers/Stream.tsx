"use client";

import React, {
  createContext,
  useContext,
  ReactNode,
  useState,
  useCallback,
  useRef,
} from "react";
import { useQueryState } from "nuqs";
import { toast } from "sonner";
import { streamChatEvents } from "@/lib/sse";
import { ApprovalPayload, ChatMessage, Role, ThinkingStep } from "@/lib/chat-types";
import { useThreads } from "./Thread";

// What web/ actually needs from a chat turn — no LangGraph Message/Checkpoint types, no
// branches, no tool-call blocks. `route`/`meta` are enough for the UI to render a citation list
// or an approval panel differently per route. `steps` is the RCA graph's per-node progress for
// this turn (empty for RAG/tool_call turns, which don't stream intermediate nodes) — kept on the
// message so it stays visible (collapsed) in history, the same way ChatGPT keeps a past turn's
// reasoning trace inspectable after the fact.
export interface DisplayMessage {
  id: string;
  turn_role: "user" | "assistant";
  content: string;
  route?: ChatMessage["route"];
  meta?: ChatMessage["meta"];
  steps?: ThinkingStep[];
}

interface StreamContextType {
  sessionId: string | null;
  role: Role;
  setRole: (role: Role) => void;
  messages: DisplayMessage[];
  isLoading: boolean;
  pendingApproval: ApprovalPayload | null;
  pendingQuestion: string | null;
  sendMessage: (text: string) => Promise<void>;
  editMessage: (id: string, text: string) => Promise<void>;
  retryMessage: (assistantId: string) => Promise<void>;
  stop: () => void;
  resumeApproval: (
    action: "approve" | "edit" | "reject",
    opts?: { reason?: string; editedDraft?: ApprovalPayload["draft"] },
  ) => Promise<void>;
  newSession: () => void;
  loadSession: (sessionId: string) => Promise<void>;
}

const StreamContext = createContext<StreamContextType | undefined>(undefined);

const DEFAULT_ROLE: Role = "sre";

export const StreamProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [sessionId, setSessionId] = useQueryState("threadId");
  const [role, setRole] = useState<Role>(DEFAULT_ROLE);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<ApprovalPayload | null>(null);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const { getSessions, setSessions } = useThreads();

  const abortRef = useRef<AbortController | null>(null);
  const reviewerName = useRef<string>(role);

  const refreshSessions = useCallback(() => {
    getSessions().then(setSessions).catch(console.error);
  }, [getSessions, setSessions]);

  const newSession = useCallback(() => {
    abortRef.current?.abort();
    setSessionId(null);
    setMessages([]);
    setPendingApproval(null);
    setPendingQuestion(null);
    setIsLoading(false);
  }, [setSessionId]);

  const loadSession = useCallback(
    async (id: string) => {
      abortRef.current?.abort();
      setPendingApproval(null);
      setPendingQuestion(null);
      const res = await fetch(`/api/chat/sessions/${id}/messages`);
      if (!res.ok) {
        toast.error("Could not load that conversation.");
        return;
      }
      const rows: ChatMessage[] = await res.json();
      setSessionId(id);
      setMessages(
        rows.map((r) => ({
          id: `${id}-${r.seq}`,
          turn_role: r.turn_role,
          content: r.content,
          route: r.route,
          meta: r.meta,
        })),
      );
    },
    [setSessionId],
  );

  // Shared by `sendMessage` (appends onto the current view) and `editMessage` (first drops
  // everything from the edited turn onward, then re-runs this). Streaming/persistence is
  // identical either way — the backend just sees another turn appended to the same session, since
  // truncating server-side history is out of scope for an edit-and-retry that only needs the
  // local view to reflect the edit going forward.
  const runTurn = useCallback(
    async (text: string, seedMessages: (prev: DisplayMessage[]) => DisplayMessage[]) => {
      const userMsg: DisplayMessage = {
        id: `local-${Date.now()}`,
        turn_role: "user",
        content: text,
      };
      setMessages((prev) => [...seedMessages(prev), userMsg]);
      setIsLoading(true);
      setPendingApproval(null);
      setPendingQuestion(null);
      reviewerName.current = role;

      const assistantId = `local-${Date.now()}-assistant`;
      let assistantText = "";
      let assistantRoute: ChatMessage["route"] | undefined;
      let assistantMeta: ChatMessage["meta"] = {};
      const assistantSteps: ThinkingStep[] = [];

      const controller = new AbortController();
      abortRef.current = controller;

      // Placeholder the assistant is streamed into — appended once, then mutated in place as
      // tokens arrive, rather than one setState per character.
      setMessages((prev) => [
        ...prev,
        { id: assistantId, turn_role: "assistant", content: "" },
      ]);

      try {
        for await (const event of streamChatEvents(
          "/api/chat/message",
          { session_id: sessionId, role, message: text },
          controller.signal,
        )) {
          switch (event.type) {
            case "session":
              setSessionId(event.session_id);
              break;
            case "route":
              assistantRoute = event.route;
              break;
            case "step":
              assistantSteps.push({ node: event.node, summary: event.summary });
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, steps: [...assistantSteps] } : m)),
              );
              break;
            case "token":
              assistantText += event.text;
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, content: assistantText } : m)),
              );
              break;
            case "refs":
              assistantMeta = { ...assistantMeta, refs: event.refs };
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, meta: assistantMeta } : m)),
              );
              break;
            case "awaiting_input":
              setPendingQuestion(event.question);
              assistantText = event.question;
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, content: assistantText } : m)),
              );
              break;
            case "awaiting_approval": {
              const { type: _t, ...payload } = event;
              setPendingApproval(payload);
              assistantText = `Draft ready for review: ${payload.draft.summary}`;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: assistantText, route: "rca", meta: { ...assistantMeta } }
                    : m,
                ),
              );
              break;
            }
            case "error":
              toast.error(event.message, { richColors: true, closeButton: true });
              break;
            case "done":
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, route: assistantRoute } : m)),
              );
              break;
          }
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          toast.error("Failed to reach sentinel-chat.", { richColors: true, closeButton: true });
        }
      } finally {
        // A user-initiated stop (see `stop` below) leaves whatever content/steps had already
        // streamed in place rather than discarding them — the investigation-so-far is still
        // useful context, it just didn't run to completion.
        if (abortRef.current === controller && abortRef.current.signal.aborted) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId && m.content === ""
                ? { ...m, content: "_Stopped._" }
                : m,
            ),
          );
        }
        setIsLoading(false);
        refreshSessions();
      }
    },
    [sessionId, role, setSessionId, refreshSessions],
  );

  const sendMessage = useCallback(
    (text: string) => runTurn(text, (prev) => prev),
    [runTurn],
  );

  // Edits a past user turn in place and re-runs it: everything from that turn onward is dropped
  // from the local view, then the edited text is sent as a fresh turn. The dropped turns are
  // still in `sentinel`'s session history (append-only, see CLAUDE.md's correctness invariants —
  // this never truncates it), so reopening the session from Conversations later shows the
  // original, un-edited trajectory; only the live view moves on from the edit.
  const editMessage = useCallback(
    (id: string, text: string) =>
      runTurn(text, (prev) => {
        const idx = prev.findIndex((m) => m.id === id);
        return idx === -1 ? prev : prev.slice(0, idx);
      }),
    [runTurn],
  );

  // Re-runs an assistant turn: finds the user message right before it and replays it through the
  // same drop-and-resend path `editMessage` uses, just with the original text instead of an edit.
  const retryMessage = useCallback(
    (assistantId: string) => {
      const assistantIdx = messages.findIndex((m) => m.id === assistantId);
      if (assistantIdx === -1) return Promise.resolve();
      const userMsg = messages
        .slice(0, assistantIdx)
        .reverse()
        .find((m) => m.turn_role === "user");
      if (!userMsg) return Promise.resolve();
      return editMessage(userMsg.id, userMsg.content);
    },
    [messages, editMessage],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const resumeApproval = useCallback(
    async (
      action: "approve" | "edit" | "reject",
      opts?: { reason?: string; editedDraft?: ApprovalPayload["draft"] },
    ) => {
      if (!pendingApproval || !sessionId) return;
      setIsLoading(true);
      try {
        const res = await fetch("/api/chat/resume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            thread_id: pendingApproval.thread_id,
            action,
            reviewer: reviewerName.current,
            reason: opts?.reason,
            edited_draft: opts?.editedDraft,
          }),
        });
        const data = await res.json();
        setMessages((prev) => [
          ...prev,
          {
            id: `resume-${Date.now()}`,
            turn_role: "assistant",
            content: `Investigation ${data.outcome}.`,
            route: "rca",
          },
        ]);
        setPendingApproval(null);
      } catch {
        toast.error("Failed to submit the review decision.", { richColors: true, closeButton: true });
      } finally {
        setIsLoading(false);
        refreshSessions();
      }
    },
    [pendingApproval, sessionId, refreshSessions],
  );

  const value: StreamContextType = {
    sessionId,
    role,
    setRole,
    messages,
    isLoading,
    pendingApproval,
    pendingQuestion,
    sendMessage,
    editMessage,
    retryMessage,
    stop,
    resumeApproval,
    newSession,
    loadSession,
  };

  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>;
};

export const useStreamContext = (): StreamContextType => {
  const context = useContext(StreamContext);
  if (context === undefined) {
    throw new Error("useStreamContext must be used within a StreamProvider");
  }
  return context;
};

export default StreamContext;

"use client";

import {
  createContext,
  useContext,
  ReactNode,
  useCallback,
  useState,
  Dispatch,
  SetStateAction,
} from "react";
import { ChatSession } from "@/lib/chat-types";

// Renamed internally from "threads" to "sessions" (matching sentinel/chat's own vocabulary —
// chat_sessions, GET /chat/sessions) but the hook keeps its original name, useThreads, since
// that's the name every consumer (Stream.tsx, history/index.tsx) already imports.
interface ThreadContextType {
  getSessions: () => Promise<ChatSession[]>;
  sessions: ChatSession[];
  setSessions: Dispatch<SetStateAction<ChatSession[]>>;
  sessionsLoading: boolean;
  setSessionsLoading: Dispatch<SetStateAction<boolean>>;
}

const ThreadContext = createContext<ThreadContextType | undefined>(undefined);

export function ThreadProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  const getSessions = useCallback(async (): Promise<ChatSession[]> => {
    const res = await fetch("/api/chat/sessions");
    if (!res.ok) return [];
    return res.json();
  }, []);

  const value: ThreadContextType = {
    getSessions,
    sessions,
    setSessions,
    sessionsLoading,
    setSessionsLoading,
  };

  return <ThreadContext.Provider value={value}>{children}</ThreadContext.Provider>;
}

export function useThreads() {
  const context = useContext(ThreadContext);
  if (context === undefined) {
    throw new Error("useThreads must be used within a ThreadProvider");
  }
  return context;
}

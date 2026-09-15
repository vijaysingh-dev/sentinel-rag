// Mirrors sentinel/chat/sse.py's ChatEvent union and sentinel/chat/db.py's chat_messages
// columns — the one place both sides of the wire agree on shape. Deliberately flat: the whole
// point of dropping @langchain/langgraph-sdk is that this app no longer needs to know what a
// LangGraph checkpoint or interrupt is, so nothing here reaches for those concepts either.

export type Role = "viewer" | "oncall" | "sre" | "admin";
export const ROLES: Role[] = ["viewer", "oncall", "sre", "admin"];

export type Route = "tool_call" | "rag" | "rca" | "refuse";

export interface ApprovalPayload {
  thread_id: string;
  draft: {
    summary: string;
    root_cause: string;
    severity: string;
    recommended_action: string;
    confidence: number;
    unresolved_questions: string[];
  };
  degraded: boolean;
  retrieval_mode: string;
  evidence: Array<{ id: string; claim: string; source_tool: string; confidence: number }>;
  usd_spent: number;
  actions: string[];
}

export interface ThinkingStep {
  node: string;
  summary: string;
}

export type ChatEvent =
  | { type: "session"; session_id: string }
  | { type: "route"; route: Route }
  | { type: "token"; text: string }
  | { type: "step"; node: string; summary: string }
  | { type: "awaiting_input"; question: string }
  | ({ type: "awaiting_approval" } & ApprovalPayload)
  | { type: "refs"; refs: string[] }
  | { type: "error"; message: string }
  | { type: "done" };

export interface ChatMessage {
  seq: number;
  turn_role: "user" | "assistant";
  content: string;
  route: Route | null;
  meta: { refs?: string[]; reason?: string; [key: string]: unknown };
  created_at: string;
}

export interface ChatSession {
  session_id: string;
  role: Role;
  title: string | null;
  updated_at: string;
}

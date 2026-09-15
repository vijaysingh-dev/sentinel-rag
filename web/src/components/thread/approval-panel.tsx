"use client";

import { useState } from "react";
import { useStreamContext } from "@/providers/Stream";
import { Button } from "../ui/button";
import { Textarea } from "../ui/textarea";
import { Label } from "../ui/label";
import { Separator } from "../ui/separator";
import { CheckCircle2, XCircle, Pencil } from "lucide-react";

// Replaces all of agent-inbox/ — that was langgraph-sdk's generic Interrupt-typed UI, built for
// an arbitrary interrupt() payload shape. sentinel/chat/api.py's awaiting_approval event is one
// specific, known shape (RCADraft + evidence + degraded + usd_spent), so this renders that
// directly rather than through a generic interrupt renderer.
export function ApprovalPanel() {
  const { pendingApproval, resumeApproval, isLoading } = useStreamContext();
  const [editing, setEditing] = useState(false);
  const [editedSummary, setEditedSummary] = useState("");
  const [rejectReason, setRejectReason] = useState("");
  const [showReject, setShowReject] = useState(false);

  if (!pendingApproval) return null;
  const { draft, degraded, evidence, usd_spent } = pendingApproval;

  return (
    <div className="mx-auto mb-4 w-full max-w-3xl rounded-2xl border border-amber-200 bg-amber-50/50 p-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-semibold text-amber-800">
          Investigation ready for review
        </span>
        {degraded && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-700">
            degraded run
          </span>
        )}
      </div>

      <div className="space-y-2 text-sm">
        <div>
          <span className="font-medium">Root cause: </span>
          {draft.root_cause}
        </div>
        <div>
          <span className="font-medium">Recommended action: </span>
          {draft.recommended_action}
        </div>
        <div className="text-gray-500">
          Severity {draft.severity} · confidence {Math.round(draft.confidence * 100)}% ·{" "}
          {evidence.length} evidence item{evidence.length === 1 ? "" : "s"} · ${usd_spent.toFixed(4)}
        </div>
        {draft.unresolved_questions.length > 0 && (
          <div>
            <span className="font-medium">Unresolved: </span>
            {draft.unresolved_questions.join("; ")}
          </div>
        )}
      </div>

      {editing && (
        <div className="mt-3 flex flex-col gap-2">
          <Label htmlFor="edited-summary">Edited summary</Label>
          <Textarea
            id="edited-summary"
            value={editedSummary}
            onChange={(e) => setEditedSummary(e.target.value)}
            className="bg-white"
          />
        </div>
      )}

      {showReject && (
        <div className="mt-3 flex flex-col gap-2">
          <Label htmlFor="reject-reason">Reason for rejecting</Label>
          <Textarea
            id="reject-reason"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            className="bg-white"
          />
        </div>
      )}

      <Separator className="my-3" />

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={isLoading}
          onClick={() => resumeApproval("approve")}
        >
          <CheckCircle2 className="size-4" />
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={isLoading}
          onClick={() => {
            if (editing) {
              resumeApproval("edit", { editedDraft: { ...draft, summary: editedSummary } });
              setEditing(false);
            } else {
              setEditedSummary(draft.summary);
              setEditing(true);
              setShowReject(false);
            }
          }}
        >
          <Pencil className="size-4" />
          {editing ? "Submit edit" : "Edit"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={isLoading}
          onClick={() => {
            if (showReject) {
              resumeApproval("reject", { reason: rejectReason });
              setShowReject(false);
            } else {
              setShowReject(true);
              setEditing(false);
            }
          }}
        >
          <XCircle className="size-4" />
          {showReject ? "Confirm reject" : "Reject"}
        </Button>
      </div>
    </div>
  );
}

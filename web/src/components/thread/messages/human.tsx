import { useState } from "react";
import { Pencil, Check, X, Copy } from "lucide-react";
import { DisplayMessage } from "@/providers/Stream";
import { useStreamContext } from "@/providers/Stream";
import { Button } from "../../ui/button";
import { Textarea } from "../../ui/textarea";
import { useCopyToClipboard } from "@/components/thread/use-copy-to-clipboard";

export function HumanMessage({ message }: { message: DisplayMessage }) {
  const { editMessage, isLoading } = useStreamContext();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const { copied, copy } = useCopyToClipboard();

  const submit = () => {
    const text = draft.trim();
    if (!text || text === message.content) {
      setEditing(false);
      return;
    }
    setEditing(false);
    editMessage(message.id, text);
  };

  if (editing) {
    return (
      <div className="ml-auto flex w-full max-w-xl flex-col items-end gap-1.5">
        <Textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            } else if (e.key === "Escape") {
              setDraft(message.content);
              setEditing(false);
            }
          }}
          className="w-full resize-none bg-white text-right"
        />
        <div className="flex items-center gap-1.5">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setDraft(message.content);
              setEditing(false);
            }}
          >
            <X className="size-3.5" />
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={submit}
          >
            <Check className="size-3.5" />
            Save &amp; retry
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="group flex flex-col items-end gap-1">
      <p className="bg-muted ml-auto w-fit rounded-3xl px-4 py-2 text-right whitespace-pre-wrap">
        {message.content}
      </p>
      <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
        <button
          type="button"
          aria-label="Copy message"
          onClick={() => copy(message.content)}
          className="text-muted-foreground hover:text-foreground p-1"
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
        </button>
        {!isLoading && (
          <button
            type="button"
            aria-label="Edit message"
            onClick={() => setEditing(true)}
            className="text-muted-foreground hover:text-foreground p-1"
          >
            <Pencil className="size-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

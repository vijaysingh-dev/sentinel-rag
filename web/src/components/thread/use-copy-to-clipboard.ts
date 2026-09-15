import { useState } from "react";

export function useCopyToClipboard({ copiedDuration = 3000 }: { copiedDuration?: number } = {}) {
  const [copied, setCopied] = useState(false);

  const copy = (value: string) => {
    if (!value) return;
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), copiedDuration);
    });
  };

  return { copied, copy };
}

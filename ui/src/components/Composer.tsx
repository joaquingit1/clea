import { ArrowUp, CornerDownLeft } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { t } from "../i18n";
import { cx } from "../lib";

/** The one input to the orchestrator, everywhere: Enter sends, Shift+Enter breaks the line. */
export function Composer({
  onSend,
  placeholder,
  size = "dock",
  autoFocus,
  busy,
  hint,
}: {
  onSend: (text: string) => Promise<void> | void;
  placeholder: string;
  size?: "hero" | "dock";
  autoFocus?: boolean;
  busy?: boolean;
  hint?: string;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = Math.min(el.scrollHeight, 260) + "px";
  }, [text]);

  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);

  const submit = async () => {
    const t = text.trim();
    if (!t || sending) return;
    setSending(true);
    setErr(null);
    try {
      await onSend(t);
      setText("");
    } catch (e) {
      setErr(String(e).replace(/^Error: /, ""));
    } finally {
      setSending(false);
      ref.current?.focus();
    }
  };

  const ready = text.trim().length > 0 && !sending;
  return (
    <div>
      <div
        className="composer-shell group relative rounded-[20px] bg-[var(--composer)] shadow-[var(--elev-composer)] backdrop-blur-[22px] backdrop-saturate-150"
        onClick={() => ref.current?.focus()}
      >
        <textarea
          ref={ref}
          value={text}
          rows={1}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={placeholder}
          className={cx(
            "block w-full resize-none bg-transparent text-fg outline-none placeholder:text-fg-3",
            size === "hero" ? "min-h-[96px] px-4 pt-3.5 pb-12 text-[15px] leading-5" : "min-h-[44px] px-4 pt-3 pb-11 text-[14px] leading-5",
          )}
        />
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center justify-between px-2.5 pb-2">
          <span className="pl-1.5 text-[12px] text-fg-3">
            {busy ? <span className="shimmer">{t("composer.thinking")}</span> : hint}
          </span>
          <button
            type="button"
            aria-label={t("composer.send")}
            onClick={(e) => {
              e.stopPropagation();
              submit();
            }}
            disabled={!ready}
            className={cx(
              "pointer-events-auto grid size-7 place-items-center rounded-full transition duration-200",
              ready ? "btn-brand" : "bg-fg/15 text-bg",
            )}
          >
            <ArrowUp size={15} strokeWidth={2.4} />
          </button>
        </div>
      </div>
      {err ? <p className="mt-2 px-2 text-[12.5px] text-red">{t("composer.failed", { err })}</p> : null}
    </div>
  );
}

export function EnterHint() {
  return (
    <span className="inline-flex items-center gap-1">
      <CornerDownLeft size={11} /> {t("chat.enter")}
    </span>
  );
}

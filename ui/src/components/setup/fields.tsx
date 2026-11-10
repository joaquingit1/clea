import { AlertTriangle, Check, Search, X } from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useId, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { t } from "../../i18n";
import { cx } from "../../lib";
import { errorText } from "../../setup";
import { Spinner } from "../ui";

/** The controls setup and settings are made of. One look for every field, button and notice, built on the app's tokens
 *  (index.css:.field,.choice,.btn-brand), so the eight steps read as one form. */

export const BTN = {
  primary: "btn-brand inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-xl px-4 text-[14px] font-medium whitespace-nowrap",
  ink: "btn-ink inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-xl px-4 text-[14px] font-medium whitespace-nowrap",
  secondary:
    "press inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-xl px-3.5 text-[14px] whitespace-nowrap text-fg shadow-[0_0_0_0.5px_var(--line-strong)] transition hover:bg-hover disabled:opacity-50",
  ghost: "inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-xl px-3 text-[14px] whitespace-nowrap text-fg-2 transition hover:bg-hover hover:text-fg disabled:opacity-50",
  small:
    "press inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded-lg px-2.5 text-[13px] whitespace-nowrap text-fg shadow-[0_0_0_0.5px_var(--line-strong)] transition hover:bg-hover disabled:opacity-50",
  ghostSmall: "inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded-lg px-2 text-[13px] whitespace-nowrap text-fg-2 transition hover:bg-hover hover:text-fg disabled:opacity-50",
  link: "inline-flex items-center gap-1 rounded-sm text-[13px] text-accent underline-offset-[3px] hover:underline disabled:opacity-50",
  linkSmall: "inline-flex items-center gap-1 rounded-sm text-[12.5px] text-accent underline-offset-[3px] hover:underline",
  // a way out that should not compete with the step's own button: "use different tokens"
  quiet: "inline-flex shrink-0 items-center gap-1 rounded-sm text-[13px] text-fg-2 underline-offset-[3px] hover:text-fg hover:underline",
};

// Each variant is spelled out, never one class overriding another: two utilities for the same property do not take
// the order they are written in.
const input = (o: { compact?: boolean; mono?: boolean }) =>
  cx("field w-full min-w-0 text-fg outline-none placeholder:text-fg-3", o.compact ? "h-8 rounded-lg px-2.5" : "h-9 rounded-xl px-3", o.mono ? "font-mono text-[13px]" : "text-[14px]");

/** A labelled control with what it is for underneath, or what is wrong with it when the worker (or a check here)
 *  refused it. The error takes the hint's place, next to the control that caused it. */
export function Field({
  label,
  hint,
  error,
  optional,
  children,
  className,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  optional?: boolean;
  children: (p: { id: string; "aria-invalid": boolean | undefined; "aria-describedby": string | undefined }) => ReactNode;
  className?: string;
}) {
  const id = useId();
  const note = error || hint ? `${id}-note` : undefined;
  return (
    <div className={className}>
      <label htmlFor={id} className="mb-1.5 flex items-baseline gap-2 text-[13px] font-medium text-fg">
        {label}
        {optional ? <span className="text-[12px] font-normal text-fg-3">{t("common.optional")}</span> : null}
      </label>
      {children({ id, "aria-invalid": error ? true : undefined, "aria-describedby": note })}
      {error ? (
        <ErrorText id={note}>{error}</ErrorText>
      ) : hint ? (
        <p id={note} className="mt-1.5 text-[12.5px] leading-snug text-fg-3">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function ErrorText({ children, id, className }: { children: ReactNode; id?: string; className?: string }) {
  return (
    <p id={id} role="alert" className={cx("mt-1.5 text-[12.5px] leading-snug text-red", className)}>
      {children}
    </p>
  );
}

export function TextInput({ className, mono, compact, ...p }: ComponentProps<"input"> & { mono?: boolean; compact?: boolean }) {
  return <input type="text" autoComplete="off" spellCheck={false} {...p} className={cx(input({ mono, compact }), className)} />;
}

/** A token or key: typed or pasted hidden, never filled in by the browser, and never read back from the worker. */
export function SecretInput({ className, ...p }: ComponentProps<"input">) {
  return (
    <input
      type="password"
      autoComplete="off"
      autoCorrect="off"
      autoCapitalize="off"
      spellCheck={false}
      data-1p-ignore
      data-lpignore="true"
      {...p}
      className={cx(input({ mono: true }), className)}
    />
  );
}

export function TextArea({ className, ...p }: ComponentProps<"textarea">) {
  return <textarea {...p} className={cx("field block w-full resize-none rounded-xl px-3 py-2.5 text-[14px] leading-5 text-fg outline-none placeholder:text-fg-3", className)} />;
}

/** `bare`: inside a row that is already a surface, the select is only its text and arrow. */
export function Select({ className, children, bare, ...p }: ComponentProps<"select"> & { bare?: boolean }) {
  return (
    <select
      {...p}
      className={cx(
        "min-w-0 appearance-none bg-[length:10px] bg-[right_9px_center] bg-no-repeat py-0 pr-7 text-fg",
        bare ? "h-7 w-[136px] shrink-0 rounded-lg bg-transparent pl-2 text-[13px] hover:bg-hover" : "field h-9 rounded-xl pl-3 text-[14px]",
        className,
      )}
      style={CHEVRON}
    >
      {children}
    </select>
  );
}
// the native arrow is gone with appearance-none; this one takes the text color at half strength in both themes
const CHEVRON = {
  backgroundImage:
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23888' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E\")",
};

/** A search box that says when it is waiting on the worker. Enter never submits the step around it. */
export function SearchInput({ value, onChange, placeholder, busy }: { value: string; onChange: (v: string) => void; placeholder: string; busy?: boolean }) {
  return (
    <label className="field flex h-9 items-center gap-2 rounded-xl px-3 text-fg-3">
      <Search size={14} className="shrink-0" />
      <input
        type="text"
        value={value}
        autoComplete="off"
        spellCheck={false}
        aria-label={placeholder}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.preventDefault();
        }}
        className="h-full w-full min-w-0 bg-transparent text-[14px] text-fg outline-none placeholder:text-fg-3"
      />
      {busy ? <Spinner size={12} /> : null}
    </label>
  );
}

export function Switch({ checked, onChange, label, hint, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; hint?: ReactNode; disabled?: boolean }) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <button
        type="button"
        role="switch"
        id={id}
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cx("relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors duration-200 disabled:opacity-50", checked ? "bg-brand" : "bg-fg/20")}
      >
        <span className={cx("absolute top-0.5 left-0.5 size-4 rounded-full bg-white shadow-[0_1px_2px_rgb(0_0_0/0.25)] transition-transform duration-200 ease-[var(--ease-spring)]", checked && "translate-x-4")} />
      </button>
      <label htmlFor={id} className="min-w-0 cursor-default text-[14px] leading-5 text-fg">
        {label}
        {hint ? <span className="mt-0.5 block text-[12.5px] leading-snug text-fg-3">{hint}</span> : null}
      </label>
    </div>
  );
}

/** Words as chips: Enter or a comma adds what is typed, Backspace on an empty box takes the last one back. Enter on an
 *  empty box is left alone, so it still continues the step. */
export function TagInput({ value, onChange, placeholder, label, upper }: { value: string[]; onChange: (v: string[]) => void; placeholder: string; label: string; upper?: boolean }) {
  const [text, setText] = useState("");
  const add = (raw: string) => {
    const words = raw
      .split(",")
      .map((w) => (upper ? w.trim().toUpperCase() : w.trim()))
      .filter(Boolean);
    if (words.length) onChange([...value, ...words.filter((w) => !value.includes(w))].filter((w, i, a) => a.indexOf(w) === i));
    setText("");
  };
  return (
    <div className="field flex min-h-9 flex-wrap items-center gap-1.5 rounded-xl px-2 py-1.5">
      {value.map((w) => (
        <span key={w} className="inline-flex h-6 items-center gap-1 rounded-md bg-hover py-0 pr-1 pl-2 text-[13px] text-fg">
          {w}
          <button type="button" aria-label={t("tags.remove", { tag: w })} onClick={() => onChange(value.filter((x) => x !== w))} className="grid size-4 place-items-center rounded text-fg-3 hover:bg-active hover:text-fg">
            <X size={11} />
          </button>
        </span>
      ))}
      <input
        type="text"
        value={text}
        aria-label={label}
        autoComplete="off"
        spellCheck={false}
        placeholder={value.length ? "" : placeholder}
        onChange={(e) => (e.target.value.includes(",") ? add(e.target.value) : setText(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === "Enter" && text.trim()) {
            e.preventDefault();
            add(text);
          } else if (e.key === "Backspace" && !text && value.length) onChange(value.slice(0, -1));
        }}
        onBlur={() => text.trim() && add(text)}
        className="h-6 min-w-[8rem] flex-1 bg-transparent px-1 text-[14px] text-fg outline-none placeholder:text-fg-3"
      />
    </div>
  );
}

/** Two or three choices side by side; the highlight slides to the one picked. */
/** `fill`: the control takes the width of its column and its choices share it equally. */
export function Segmented<T extends string>({ value, onChange, options, label, name, fill }: { value: T; onChange: (v: T) => void; options: { value: T; label: string }[]; label: string; name: string; fill?: boolean }) {
  return (
    <div role="radiogroup" aria-label={label} className={cx("max-w-full gap-0.5 rounded-xl bg-hover p-0.5", fill ? "flex w-full" : "inline-flex")}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(o.value)}
            className={cx("relative h-8 min-w-0 rounded-[10px] px-3 text-[13.5px] transition-colors sm:px-3.5", fill && "flex-1", on ? "text-fg" : "text-fg-2 hover:text-fg")}
          >
            {on ? (
              <motion.span layoutId={`seg-${name}`} transition={{ type: "spring", duration: 0.3, bounce: 0.05 }} className="absolute inset-0 rounded-[10px] bg-panel shadow-[var(--shadow),0_0_0_0.5px_var(--line)]" />
            ) : null}
            <span className="relative">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Something the user should know before going on: amber for "this will not fully work", red for "this failed". */
export function Notice({ tone = "amber", title, children, className }: { tone?: "amber" | "red" | "neutral"; title?: ReactNode; children?: ReactNode; className?: string }) {
  const c = { amber: "bg-amber-soft text-amber", red: "bg-red-soft text-red", neutral: "bg-hover text-fg-2" }[tone];
  return (
    <div className={cx("flex gap-2.5 rounded-xl px-3 py-2.5 text-[13px] leading-snug", c, className)}>
      {tone !== "neutral" ? <AlertTriangle size={15} className="mt-px shrink-0" /> : null}
      <div className="min-w-0 flex-1">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className={cx("text-fg-2", !!title && "mt-1")}>{children}</div> : null}
      </div>
    </div>
  );
}

/** "Connected to Acme as ada": one calm line with a check, and what can be done about it on the right. */
export function Connected({ children, detail, action }: { children: ReactNode; detail?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-start gap-2.5 rounded-xl bg-green-soft px-3 py-2.5">
      <span className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-green text-white">
        <Check size={11} strokeWidth={3} />
      </span>
      <div className="min-w-0 flex-1 text-[13.5px] leading-snug">
        <p className="text-fg [overflow-wrap:anywhere]">{children}</p>
        {detail ? <p className="mt-0.5 text-[12.5px] text-fg-2">{detail}</p> : null}
      </div>
      {action}
    </div>
  );
}

/** The heading of one part of a step. */
export function Part({ title, lead, right, children }: { title: ReactNode; lead?: ReactNode; right?: ReactNode; children: ReactNode }) {
  return (
    <section>
      <div className="mb-2.5 flex items-end gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-[14px] font-semibold text-fg">{title}</h2>
          {lead ? <p className="mt-0.5 text-[13px] leading-snug text-fg-2">{lead}</p> : null}
        </div>
        {right}
      </div>
      {children}
    </section>
  );
}

/** The value, a beat after the user stops changing it: one request per pause in typing, not one per key. */
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

/** Runs `load` for the given key and keeps only the answer to the latest one, so a slow early search never lands on top
 *  of a later one. `data` stays on screen while the next answer is on its way. */
export function useLoad<T>(key: string | null, load: () => Promise<T>): { data: T | null; busy: boolean; failed: boolean; error: string | null; retry: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const fn = useRef(load);
  fn.current = load;
  useEffect(() => {
    if (key === null) return;
    let alive = true;
    setBusy(true);
    fn.current()
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(null);
      })
      .catch((e) => alive && setError(errorText(e)))
      .finally(() => alive && setBusy(false));
    return () => {
      alive = false;
    };
  }, [key, nonce]);
  return { data, busy, failed: error !== null, error, retry: () => setNonce((n) => n + 1) };
}

/** Brings an element fully into view inside the step that scrolls it, and moves nothing else (scrollIntoView would also
 *  shift every clipped box around it, the card included). */
export function reveal(el: HTMLElement | null) {
  const box = el?.closest<HTMLElement>(".overflow-y-auto");
  if (!el || !box) return;
  const a = el.getBoundingClientRect(), b = box.getBoundingClientRect();
  if (a.bottom > b.bottom) box.scrollBy({ top: Math.min(a.bottom - b.bottom + 12, a.top - b.top - 12), behavior: "smooth" });
  else if (a.top < b.top) box.scrollBy({ top: a.top - b.top - 12, behavior: "smooth" });
}

/** A list that could not be loaded: what happened and a way to ask again, in the list's own place. */
export function LoadFailed({ children, retry }: { children: ReactNode; retry: () => void }) {
  return (
    <div className="flex items-center gap-3 px-3 py-3 text-[13px] text-fg-2">
      <span className="min-w-0 flex-1">{children}</span>
      <button type="button" onClick={retry} className={BTN.small}>
        {t("common.retry")}
      </button>
    </div>
  );
}

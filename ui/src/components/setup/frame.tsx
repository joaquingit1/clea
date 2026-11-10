import { ArrowLeft, Check } from "lucide-react";
import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { t } from "../../i18n";
import { cx } from "../../lib";
import { errorText } from "../../setup";
import { Spinner } from "../ui";
import { BTN, ErrorText } from "./fields";

/** Where a step is being shown. The same step components serve both: in the onboarding a step fills the window and
 *  "Continue" saves it and moves on; in settings it is a section that saves by itself. */
export interface FrameCtx {
  mode: "onboarding" | "settings";
  /** onboarding: go to the step before this one (absent on the first) */
  back?: () => void;
  /** onboarding: this step is saved, move on */
  done: () => void;
  /** onboarding: said once at the top when "Start Clea" was refused and sent the user back here */
  sentBack?: boolean;
}
const Ctx = createContext<FrameCtx>({ mode: "settings", done: () => {} });
export const FrameProvider = Ctx.Provider;
export const useFrame = () => useContext(Ctx);

/** Thrown by a step's save when a field is wrong and already says so itself: the frame stays put and moves focus to the
 *  first field marked invalid, without a second message of its own. */
export class Invalid extends Error {}

/** A step: its title, its fields, and the one button that saves it. `save` does the POST and throws on failure (the
 *  worker's text shows by the button that was pressed); resolving to "stay" keeps the user on the step, for a button
 *  that connects or checks instead of continuing. Whatever was typed stays typed. */
export function StepFrame({
  title,
  lead,
  hero,
  children,
  save,
  ready = true,
  blocked,
  dirty = true,
  submit,
  skip,
  wide,
  focus = true,
}: {
  title: string;
  lead: ReactNode;
  /** an illustration above a centered title (the welcome step) */
  hero?: ReactNode;
  children: ReactNode;
  save?: () => Promise<void | "stay">;
  /** false: the button is disabled and `blocked` says why */
  ready?: boolean;
  blocked?: string;
  /** settings: something changed since the last save */
  dirty?: boolean;
  /** the button's label when it is not "Continue" / "Save" */
  submit?: string;
  /** onboarding: an optional step can be left for later */
  skip?: boolean;
  wide?: boolean;
  /** false: a step of cards, where putting the caret in the first box would point at one choice among several */
  focus?: boolean;
}) {
  const f = useFrame();
  const form = useRef<HTMLFormElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [refused, setRefused] = useState(0);

  // the first field of a step takes focus as the step arrives, so typing and Enter work at once
  useEffect(() => {
    if (f.mode !== "onboarding" || !focus) return;
    const el = form.current?.querySelector<HTMLElement>("[data-step-body] :is(input, textarea):not([disabled])");
    el?.focus({ preventScroll: true });
  }, [f.mode, focus]);

  // Enter continues even when nothing has focus (a step of cards has no field to press Enter in)
  useEffect(() => {
    if (f.mode !== "onboarding") return;
    const on = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      const a = document.activeElement;
      if (a && a !== document.body) return; // a focused control handles its own Enter
      form.current?.requestSubmit();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [f.mode]);

  // a field refused the save: the caret goes to the first one marked wrong, once the step has drawn its message
  useEffect(() => {
    if (refused) form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [refused]);

  useEffect(() => {
    if (!saved) return;
    const h = setTimeout(() => setSaved(false), 2400);
    return () => clearTimeout(h);
  }, [saved]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!save || !ready || busy) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const r = await save();
      if (r === "stay") return;
      if (f.mode === "onboarding") f.done();
      else setSaved(true);
    } catch (err) {
      if (err instanceof Invalid) setRefused((n) => n + 1);
      else setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const note = error ? (
    <ErrorText className="mt-0">{error}</ErrorText>
  ) : !ready && blocked ? (
    <p className="text-[12.5px] leading-snug text-fg-2">{blocked}</p>
  ) : null;

  if (f.mode === "settings") {
    return (
      <form ref={form} onSubmit={onSubmit} noValidate>
        {lead ? <p className="mb-5 text-[13px] leading-snug text-fg-2">{lead}</p> : null}
        <div data-step-body className="space-y-7">
          {children}
        </div>
        {save ? (
          <div className="mt-6 flex flex-wrap items-center gap-x-3 gap-y-2">
            <button type="submit" disabled={!ready || busy || (!dirty && !submit)} className={BTN.primary}>
              {busy ? <Spinner size={13} className="text-white" /> : null}
              {submit ?? t("common.save")}
            </button>
            {saved ? (
              <span className="fade-in inline-flex items-center gap-1.5 text-[13px] text-green">
                <Check size={14} /> {t("common.saved")}
              </span>
            ) : (
              <div className="min-w-0 flex-1">{note}</div>
            )}
          </div>
        ) : null}
      </form>
    );
  }

  return (
    <form ref={form} onSubmit={onSubmit} noValidate className="flex min-h-0 flex-1 flex-col">
      <div className={cx("min-h-0 flex-1 overflow-y-auto", !!hero && "flex flex-col")}>
        {/* a step with a hero is short: it sits in the middle of the card (my-auto), and still scrolls when the window is small */}
        <div className={cx("mx-auto w-full px-5 pt-6 pb-7 sm:px-10 sm:pt-9", wide ? "max-w-[720px]" : "max-w-[640px]", !!hero && "my-auto sm:pb-12")}>
          {f.sentBack ? <p className="mb-4 rounded-xl bg-amber-soft px-3 py-2 text-[13px] text-amber">{t("review.sentBack")}</p> : null}
          <header className={cx(!!hero && "flex flex-col items-center text-center")}>
            {hero ? <div className="mb-5">{hero}</div> : null}
            <h1 className="text-[24px] leading-[1.875rem] font-semibold tracking-[-0.02em] text-balance">{title}</h1>
            <p className={cx("mt-2 text-[14px] leading-[1.5] text-pretty text-fg-2", !!hero && "max-w-[460px]")}>{lead}</p>
          </header>
          <div data-step-body className={cx("mt-7 space-y-7", !!hero && "mx-auto max-w-[380px]")}>
            {children}
          </div>
        </div>
      </div>
      <footer className="shrink-0 border-t-[0.5px] border-line-strong px-5 py-3 sm:px-10">
        {note ? <div className="mb-2.5 text-right">{note}</div> : null}
        <div className="flex items-center gap-2">
          {f.back ? (
            <button type="button" onClick={f.back} className={cx(BTN.ghost, "-ml-2")}>
              <ArrowLeft size={15} /> {t("common.back")}
            </button>
          ) : null}
          <span className="flex-1" />
          {skip ? (
            <button type="button" onClick={f.done} className={BTN.ghost}>
              {t("common.skip")}
            </button>
          ) : null}
          <button type="submit" disabled={!ready || busy || !save} className={BTN.primary}>
            {busy ? <Spinner size={13} className="text-white" /> : null}
            {submit ?? t("common.continue")}
          </button>
        </div>
      </footer>
    </form>
  );
}

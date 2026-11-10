import { ChevronDown, RotateCcw } from "lucide-react";
import { useState } from "react";
import { t } from "../i18n";
import { cx } from "../lib";
import { href } from "../router";
import { api, errorText, refreshSetup, type SetupState, type StepId } from "../setup";
import { BTN, ErrorText } from "./setup/fields";
import { FrameProvider } from "./setup/frame";
import { STEPS } from "./setup/steps";
import { Card, Spinner } from "./ui";

/** Settings: the onboarding's steps again, each as a section that opens in place and saves on its own, so changing one
 *  thing never means walking the whole setup. The sections and their one-line summaries come from the worker. */
export function Settings({ state }: { state: SetupState | null }) {
  const [open, setOpen] = useState<Set<StepId>>(() => new Set(["welcome"]));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!state)
    return (
      <div className="grid h-full place-items-center px-8 text-center text-[14px] text-fg-2">
        <p>{t("settings.failed")}</p>
      </div>
    );

  const toggle = (id: StepId) =>
    setOpen((o) => {
      const n = new Set(o);
      if (!n.delete(id)) n.add(id);
      return n;
    });
  const rerun = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.reopen();
      // The address goes first and quietly (no hashchange): the onboarding then opens on its first step, not on wherever
      // the last run ended, and the app underneath never sees a setup address it would send back to the board.
      window.history.pushState(null, "", href({ page: "setup", step: "welcome" }));
      await refreshSetup();
    } catch (e) {
      if (window.location.hash.startsWith("#/setup")) window.history.replaceState(null, "", href({ page: "settings" }));
      setError(errorText(e));
      setBusy(false);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-[760px] px-3 pt-10 pb-20 sm:px-8">
        <header className="rise">
          <h1 className="text-[22px] font-semibold tracking-[-0.02em]">{t("settings.title")}</h1>
          <p className="mt-1 text-[14px] text-fg-2">{t("settings.lead")}</p>
        </header>
        <FrameProvider value={{ mode: "settings", done: () => {} }}>
          <div className="rise rise-1 mt-7 space-y-2.5">
            {state.steps.map((s) => {
              const Step = STEPS[s.id];
              const on = open.has(s.id);
              return (
                <Card key={s.id}>
                  <h2>
                    <button type="button" aria-expanded={on} onClick={() => toggle(s.id)} className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-hover">
                      <span className="min-w-0 flex-1">
                        <span className="block text-[14px] font-semibold text-fg">{s.id === "welcome" ? t("settings.profile") : t(`step.${s.id}`)}</span>
                        {s.summary && !on ? <span className="mt-0.5 block truncate text-[13px] font-normal text-fg-3">{s.summary}</span> : null}
                        {!s.summary && !s.done && !on ? <span className="mt-0.5 block text-[13px] font-normal text-fg-3">{t(s.optional ? "review.skipped" : "review.notSet")}</span> : null}
                      </span>
                      <ChevronDown size={15} className={cx("shrink-0 text-fg-3 transition", on && "rotate-180")} />
                    </button>
                  </h2>
                  {on ? (
                    <div className="fade-in border-t-[0.5px] border-line-strong px-4 pt-4 pb-5">
                      <Step state={state} />
                    </div>
                  ) : null}
                </Card>
              );
            })}
          </div>
        </FrameProvider>

        <div className="rise rise-2 mt-10 flex flex-wrap items-center gap-x-4 gap-y-2 border-t-[0.5px] border-line-strong pt-6">
          <div className="min-w-0 flex-1">
            <p className="text-[13px] text-fg-2">{t("settings.rerunHint")}</p>
            {error ? <ErrorText>{error}</ErrorText> : null}
          </div>
          <button type="button" onClick={rerun} disabled={busy} className={BTN.secondary}>
            {busy ? <Spinner size={13} /> : <RotateCcw size={14} className="text-fg-2" />}
            {t("settings.rerun")}
          </button>
        </div>
      </div>
    </div>
  );
}

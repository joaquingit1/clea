import { Check, ChevronRight } from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { t } from "../../i18n";
import { cx } from "../../lib";
import { go, swap, useRoute } from "../../router";
import { SetupError, api, currentSetup, type SetupState, type SetupStep, type StepId } from "../../setup";
import { Lockup, Mark } from "../ui";
import { FrameProvider, StepFrame, type FrameCtx } from "./frame";
import { Starting, Window } from "./shell";
import { STEPS } from "./steps";

/** A place in the onboarding: one of the worker's steps, or the review that ends it. */
type Place = StepId | "review";

/** The worker's steps in its order, then the review. */
function places(s: SetupState): Place[] {
  return [...s.steps.map((x) => x.id), "review"];
}
/** A place can be opened once every required step before it is done: nothing later depends on an optional one. */
function reachable(s: SetupState, p: Place): boolean {
  const i = places(s).indexOf(p);
  return i >= 0 && s.steps.slice(0, i).every((x) => x.done || x.optional);
}
/** Where to pick up: the first required step still to do, or the review when none is. */
function resume(s: SetupState): Place {
  return s.steps.find((x) => !x.done && !x.optional)?.id ?? "review";
}
function resolve(s: SetupState, asked: string | null): Place {
  return asked && reachable(s, asked as Place) ? (asked as Place) : resume(s);
}

/** First run: the whole window, one step at a time, over the sky. Each step saves on "Continue" and reads what it
 *  shows from the worker, so the user can go back, or quit and return to where they were. The step is in the address
 *  (#/setup/slack), so Back and Forward move between steps. */
export function Onboarding({ state }: { state: SetupState }) {
  const route = useRoute();
  const asked = route.page === "setup" ? route.step : null;
  const [current, setCurrent] = useState<Place>(() => resolve(state, asked));
  const [starting, setStarting] = useState(false);
  const [sentBack, setSentBack] = useState<Place | null>(null);

  // The place is settled when the address changes, never while a step is being filled in: a refresh of the state under
  // the user must not move them (and lose what they typed). So the state is read as it is at that moment, not depended on.
  useEffect(() => {
    const next = resolve(currentSetup() ?? state, asked);
    setCurrent(next);
    if (asked !== next) swap({ page: "setup", step: next });
  }, [asked, route.page]);

  const all = places(state);
  const i = all.indexOf(current);
  const goto = (p: Place) => {
    setSentBack(null);
    go({ page: "setup", step: p });
  };
  const frame: FrameCtx = {
    mode: "onboarding",
    back: i > 0 ? () => goto(all[i - 1]) : undefined,
    done: () => {
      const now = currentSetup() ?? state;
      const next = all[i + 1] ?? "review";
      // saved, yet the worker still counts this step as not done: say so here instead of silently staying put
      if (!reachable(now, next)) setSentBack(current);
      else goto(next);
    },
    sentBack: sentBack === current,
  };

  if (starting)
    return (
      <Window>
        <Starting />
      </Window>
    );

  const Step = current === "review" ? null : STEPS[current];
  const first = current === "welcome";
  return (
    <Window>
      {/* the welcome is a small card on the window's axis; the steps after it get the rail and the full card */}
      <div className={cx("glass-strong page-in flex w-full overflow-hidden rounded-3xl", first ? "max-h-full max-w-[480px]" : "h-full max-h-[720px] max-w-[1000px]")}>
        {first ? null : <Rail state={state} current={current} onPick={goto} />}
        <div className="flex min-w-0 flex-1 flex-col">
          {first ? null : <Progress state={state} current={current} onPick={goto} />}
          <FrameProvider value={frame}>
            {/* one key per step: the step arrives with the app's entrance, and its fields start from what is saved */}
            <div key={current} className="page-in flex min-h-0 flex-1 flex-col">
              {Step ? (
                <Step state={state} />
              ) : (
                <Review
                  state={state}
                  onPick={goto}
                  onStarted={() => setStarting(true)}
                  onMissing={(first) => {
                    setSentBack(first);
                    go({ page: "setup", step: first });
                  }}
                />
              )}
            </div>
          </FrameProvider>
        </div>
      </div>
    </Window>
  );
}

/** The steps down the left, from the worker's list: done, here, or still to come. A step that can be opened is a
 *  button. Wide windows only; a narrow one gets the bar on top. */
function Rail({ state, current, onPick }: { state: SetupState; current: Place; onPick: (p: Place) => void }) {
  return (
    <nav aria-label={t("setup.steps")} className="hidden w-[244px] shrink-0 flex-col border-r-[0.5px] border-line-strong bg-[color-mix(in_oklab,var(--sidebar)_50%,transparent)] md:flex">
      <div className="px-5 pt-5 pb-1">
        <Lockup height={17} state="intro" className="text-brand-ink" />
      </div>
      <ol className="mt-4 min-h-0 flex-1 space-y-px overflow-y-auto px-2.5 pb-4">
        {places(state).map((p, n) => {
          const step = state.steps.find((x) => x.id === p);
          const on = p === current;
          const can = reachable(state, p);
          return (
            <li key={p}>
              <button
                type="button"
                disabled={!can}
                aria-current={on ? "step" : undefined}
                onClick={() => onPick(p)}
                className={cx("relative isolate flex w-full items-start gap-2.5 rounded-[10px] px-2.5 py-[7px] text-left transition", can && !on && "hover:bg-hover", !can && "opacity-60")}
              >
                {on ? <motion.span layoutId="setup-step" transition={{ type: "spring", stiffness: 520, damping: 42, mass: 0.8 }} className="absolute inset-0 -z-10 rounded-[10px] bg-active" /> : null}
                <Dot n={n + 1} done={!!step?.done} on={on} />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13.5px] leading-[18px] text-fg">{t(`step.${p}`)}</span>
                  <Under step={step} on={on} />
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** What is saved for a step, in the worker's one line; "optional" for a step that can be skipped and has nothing yet. */
function Under({ step, on }: { step: SetupStep | undefined; on: boolean }) {
  const text = step?.done && step.summary && !on ? step.summary : step?.optional && !step.done ? t("setup.optional") : "";
  return text ? <span className="block truncate text-[12px] leading-4 text-fg-3">{text}</span> : null;
}

function Dot({ n, done, on }: { n: number; done: boolean; on: boolean }) {
  return (
    <span
      className={cx(
        "mt-px grid size-[18px] shrink-0 place-items-center rounded-full text-[10.5px] font-medium tabular-nums transition-colors",
        done ? "bg-green text-white" : on ? "bg-brand text-white" : "text-fg-3 shadow-[inset_0_0_0_1px_var(--line-strong)]",
      )}
    >
      {done ? <Check size={11} strokeWidth={3.2} /> : n}
    </span>
  );
}

/** The same steps as a bar, for a window too narrow for the rail: where the user is, and a segment per step. */
function Progress({ state, current, onPick }: { state: SetupState; current: Place; onPick: (p: Place) => void }) {
  const all = places(state);
  const i = all.indexOf(current);
  return (
    <div className="shrink-0 px-5 pt-4 sm:px-10 md:hidden">
      <p className="flex items-center gap-2 text-[12.5px] text-fg-3">
        <Mark size={15} className="text-brand-ink" />
        <span>{t("setup.progress", { n: i + 1, total: all.length })}</span>
        <span className="text-fg-2">· {t(`step.${current}`)}</span>
      </p>
      <ol aria-label={t("setup.steps")} className="mt-1.5 flex gap-1">
        {all.map((p, n) => {
          const step = state.steps.find((x) => x.id === p);
          const can = reachable(state, p);
          return (
            <li key={p} className="flex-1">
              <button type="button" disabled={!can} aria-label={t(`step.${p}`)} aria-current={p === current ? "step" : undefined} onClick={() => onPick(p)} className="block h-4 w-full rounded-sm">
                <span className={cx("block h-[3px] rounded-full transition-colors", p === current ? "bg-brand-ink" : step?.done ? "bg-green" : n < i ? "bg-fg-3" : "bg-line-strong")} />
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** Everything that was set up, a line per step, each one a way back to change it; then the button that starts Clea.
 *  If the worker refuses because a step is not done, the user is taken to the first one it names. */
function Review({ state, onPick, onStarted, onMissing }: { state: SetupState; onPick: (p: Place) => void; onStarted: () => void; onMissing: (first: StepId) => void }) {
  return (
    <StepFrame
      title={t("review.title")}
      lead={t("review.lead")}
      submit={t("review.start")}
      save={async () => {
        try {
          await api.finish();
        } catch (e) {
          if (!(e instanceof SetupError) || !e.missing.length) throw e;
          const order = state.steps.map((x) => x.id);
          onMissing([...e.missing].sort((a, b) => order.indexOf(a) - order.indexOf(b))[0]);
          return "stay";
        }
        onStarted();
        return "stay";
      }}
    >
      <ul className="overflow-hidden rounded-2xl shadow-[0_0_0_0.5px_var(--line-strong)]">
        {state.steps.map((s, i) => (
          <li key={s.id} className={cx(i > 0 && "border-t-[0.5px] border-line-strong")}>
            <button type="button" onClick={() => onPick(s.id)} className="group flex w-full items-center gap-3 px-3.5 py-2.5 text-left transition hover:bg-hover">
              <span className={cx("grid size-[18px] shrink-0 place-items-center rounded-full", s.done ? "bg-green text-white" : s.optional ? "shadow-[inset_0_0_0_1px_var(--line-strong)]" : "bg-amber-soft")}>
                {s.done ? <Check size={11} strokeWidth={3.2} /> : !s.optional ? <span className="size-1.5 rounded-full bg-amber" /> : null}
              </span>
              <span className="w-[84px] shrink-0 text-[13.5px] font-medium text-fg">{t(`step.${s.id}`)}</span>
              <span className={cx("min-w-0 flex-1 truncate text-[13.5px]", s.done ? "text-fg-2" : s.optional ? "text-fg-3" : "text-amber")}>
                {s.done ? s.summary : s.optional ? t("review.skipped") : t("review.notSet")}
              </span>
              <ChevronRight size={15} className="shrink-0 text-fg-3 opacity-0 transition group-hover:opacity-100 group-focus-visible:opacity-100" />
            </button>
          </li>
        ))}
      </ul>
      {state.steps.some((s) => !s.done && !s.optional) ? <p className="text-[13px] text-fg-2">{t("review.missing")}</p> : null}
    </StepFrame>
  );
}

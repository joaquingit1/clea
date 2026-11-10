import { Check, Minus } from "lucide-react";
import { useState } from "react";
import { t, type Key } from "../../i18n";
import { cx } from "../../lib";
import { api, refreshSetup, type Autonomy, type SetupState } from "../../setup";
import { Tag } from "../ui";
import { StepFrame } from "./frame";

// what each level does by itself, and what it never does without the user: the worker's three levels, in plain words
const LEVELS: { id: Autonomy; does: Key[]; not: Key[] }[] = [
  { id: "watch", does: ["autonomy.watch.does.1", "autonomy.watch.does.2"], not: ["autonomy.watch.not.1", "autonomy.watch.not.2"] },
  { id: "ask", does: ["autonomy.ask.does.1", "autonomy.ask.does.2"], not: ["autonomy.ask.not.1", "autonomy.ask.not.2"] },
  { id: "auto", does: ["autonomy.auto.does.1", "autonomy.auto.does.2", "autonomy.auto.does.3"], not: ["autonomy.auto.not.1", "autonomy.auto.not.2"] },
];

/** How much Clea may do without asking. "Ask first" is the recommendation, and what a new install starts on. */
export function AutonomyStep({ state }: { state: SetupState }) {
  const fresh = !state.steps.find((x) => x.id === "autonomy")?.done;
  const [level, setLevel] = useState<Autonomy>(fresh ? "ask" : state.autonomy);
  return (
    <StepFrame
      title={t("autonomy.title")}
      lead={t("autonomy.lead")}
      save={async () => {
        await api.autonomy(level);
        await refreshSetup();
      }}
      dirty={level !== state.autonomy}
      wide
    >
      <div role="radiogroup" aria-label={t("autonomy.title")} className="space-y-2.5">
        {LEVELS.map((l) => {
          const on = l.id === level;
          return (
            <button key={l.id} type="button" role="radio" aria-checked={on} data-on={on || undefined} onClick={() => setLevel(l.id)} className="choice block w-full rounded-2xl px-4 py-3.5 text-left">
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className={cx("grid size-4 shrink-0 place-items-center rounded-full transition", on ? "bg-brand" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]")}>
                  {on ? <span className="size-1.5 rounded-full bg-white" /> : null}
                </span>
                <span className="text-[14px] font-semibold text-fg">{t(`autonomy.${l.id}.name`)}</span>
                {l.id === "ask" ? <Tag tone="accent">{t("autonomy.recommended")}</Tag> : null}
                <span className="w-full text-[13px] text-fg-2 sm:ml-1 sm:w-auto">{t(`autonomy.${l.id}.tagline`)}</span>
              </span>
              <span className="mt-3 grid gap-x-6 gap-y-3 pl-6 sm:grid-cols-2">
                <List title={t("autonomy.does")} items={l.does} yes />
                <List title={t("autonomy.doesNot")} items={l.not} />
              </span>
            </button>
          );
        })}
      </div>
    </StepFrame>
  );
}

function List({ title, items, yes }: { title: string; items: Key[]; yes?: boolean }) {
  return (
    <span className="block">
      <span className="mb-1 block text-[12px] font-medium text-fg-3">{title}</span>
      {items.map((k) => (
        <span key={k} className="flex gap-1.5 py-0.5 text-[13px] leading-snug text-fg-2">
          {yes ? <Check size={13} strokeWidth={2.4} className="mt-[3px] shrink-0 text-green" /> : <Minus size={13} strokeWidth={2.4} className="mt-[3px] shrink-0 text-fg-3" />}
          <span>{t(k)}</span>
        </span>
      ))}
    </span>
  );
}

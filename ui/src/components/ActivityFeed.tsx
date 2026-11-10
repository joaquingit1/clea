import { useMemo } from "react";
import { usePoll, type Activity } from "../api";
import { t, useLang } from "../i18n";
import { clock, cx, dayLabel } from "../lib";
import { href } from "../router";
import { Mark, Tag } from "./ui";

/** Everything the agent did, newest first, each step with the case it belongs to and the reason it gave. */
export function ActivityFeed() {
  const { data } = usePoll<Activity[]>("/api/activity", 8000);
  const lang = useLang(); // the day labels are text: regroup when the language changes
  // consecutive steps of the same case collapse into one row: the case, its steps in order, and the reasons given
  const groups = useMemo(() => {
    const out: { day: string; items: Run[] }[] = [];
    for (const a of data ?? []) {
      const d = dayLabel(a.at);
      if (!out.length || out[out.length - 1].day !== d) out.push({ day: d, items: [] });
      const items = out[out.length - 1].items;
      const last = items[items.length - 1];
      if (last && last.id === a.id) last.steps.unshift(a);
      else items.push({ id: a.id, headline: a.headline, kind: a.kind, at: a.at, steps: [a] });
    }
    return out;
  }, [data, lang]);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[820px] px-8 pt-12 pb-20">
        <h1 className="text-[22px] font-semibold tracking-[-0.02em]">{t("activity.title")}</h1>
        <p className="mt-1 text-[14px] text-fg-2">{t("activity.lead")}</p>
        {!data ? (
          <div className="mt-10 flex items-center gap-3">
            <Mark size={16} busy className="text-brand-ink" />
            <span className="shimmer">{t("common.loading")}</span>
          </div>
        ) : null}
        <div className="mt-8 space-y-8">
          {groups.map((g) => (
            <section key={g.day}>
              <p className="glass sticky top-0 z-10 -mx-2 inline-block rounded-lg px-2.5 py-1 text-[12px] font-medium text-fg-2">{g.day}</p>
              <div className="mt-1">
                {g.items.map((r, i) => {
                  const last = r.steps[r.steps.length - 1];
                  const notes = [...new Set(r.steps.map((x) => x.note).filter(Boolean))];
                  return (
                    <a
                      key={i}
                      href={href({ page: "thread", id: r.id })}
                      className="group -mx-3 flex gap-4 rounded-lg px-3 py-3 transition hover:bg-hover"
                    >
                      <span className="w-11 shrink-0 pt-px font-mono text-[12px] text-fg-3 tabular-nums">{clock(r.at)}</span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="truncate text-[14px] font-medium text-fg">{r.headline}</p>
                          <Tag>{r.kind}</Tag>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[13px]">
                          {r.steps.map((x, j) => (
                            <span key={j} className="flex items-center gap-1.5">
                              {j ? <span className="text-fg-3">→</span> : null}
                              <span className={cx(x === last ? tone(x.stage) : "text-fg-3", x === last && "font-medium")}>{x.title}</span>
                            </span>
                          ))}
                        </div>
                        {notes.length ? (
                          <p className="mt-1 line-clamp-2 text-[13px] text-fg-2">{notes.join(" · ")}</p>
                        ) : null}
                      </div>
                    </a>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

interface Run {
  id: number;
  headline: string;
  kind: string;
  at: number;
  steps: Activity[];
}

function tone(stage: string): string {
  if (stage === "done") return "text-green";
  if (stage === "blocked") return "text-red";
  if (stage.startsWith("awaiting")) return "text-amber";
  return "text-fg-2";
}

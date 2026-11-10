import { ArrowLeft, CheckCircle2, GitMerge, MessageSquare, XCircle } from "lucide-react";
import { usePoll, type Board, type OwnPr, type ThreadDetail } from "../api";
import { t as tr, tn } from "../i18n"; // tr: in this file t is a thread
import { ago, clock, cx, dayLabel, githubPr } from "../lib";
import { href } from "../router";
import { PrActions } from "./PrActions";
import { CiDot, Glyph, Tag, TicketLink } from "./ui";

/** One of his PRs on its own screen: what it is, why it is ready, the case behind it, and the only place with Merge. */
export function PrView({ repo, n, board }: { repo: string; n: number; board: Board | null }) {
  const pr = board?.prs.find((p) => p.repo === repo && p.number === n) ?? null;
  const { data: t } = usePoll<ThreadDetail>(pr?.case ? `/api/thread/${pr.case}` : null, 15000);
  if (!board) return <div className="grid h-full place-items-center text-[13px] text-fg-3"><span className="shimmer">{tr("common.loading")}</span></div>;
  if (!pr)
    return (
      <div className="grid h-full place-items-center text-center text-[14px] text-fg-2">
        <div>
          <p>{tr("pr.gone", { pr: `${repo}#${n}` })}</p>
          <a href={href({ page: "board" })} className="mt-2 inline-block text-accent hover:underline">{tr("pr.backHome")}</a>
        </div>
      </div>
    );
  const human = pr.approval === "human";
  const ready = pr.state === "merge";
  // the PRs this one waits on or collides with, each opening its own screen
  const related = [...new Set([...pr.after, ...(pr.why.match(/[\w-]+#\d+/g) ?? [])])].map((label) => {
    const [r, k] = label.split("#");
    const o = board.prs.find((x) => x.label === label);
    return { label, repo: r, n: Number(k), mine: !!o, why: o ? (o.state === "merge" ? tr("pr.readyShort") : o.why).slice(0, 70) : "" };
  });
  const firstUp = board.prs.filter((o) => pr.after.includes(o.label) && o.state === "merge");
  const days = pr.created ? Math.floor((Date.now() - Date.parse(pr.created)) / 86400000) : 0;
  return (
    <div className="flex h-full min-w-0 flex-col">
      <div data-drag className="flex h-[46px] shrink-0 items-center gap-2 border-b-[0.5px] border-line-strong px-3">
        <a href={href({ page: "board" })} className="grid size-7 place-items-center rounded-md text-fg-3 hover:bg-hover hover:text-fg" aria-label={tr("common.back")}>
          <ArrowLeft size={15} />
        </a>
        <span className="truncate text-[13px] text-fg-2">{pr.label}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[860px] px-8 py-10">
          <div className="flex flex-wrap items-center gap-1.5">
            {ready ? (
              <Tag tone={human ? "green" : "amber"}>
                <GitMerge size={12} /> {tr(human ? "pr.ready" : "pr.readyBot")}
              </Tag>
            ) : (
              <Tag>{pr.why}</Tag>
            )}
            <Tag mono>{pr.label}</Tag>
            {pr.ci ? (
              <Tag>
                <CiDot ci={pr.ci} /> {tr(`ci.${pr.ci}`)}
              </Tag>
            ) : null}
            {t?.ticket ? <TicketLink id={t.ticket} /> : null}
          </div>
          <h1 className="mt-4 text-[24px] leading-[1.75rem] font-semibold text-balance first-letter:uppercase">{pr.title}</h1>
          <p className="mt-2 text-[13.5px] text-fg-2">
            {days ? tn("pr.opened", days) : tr("pr.openedToday")}
            {pr.after.length && pr.state === "queued" ? tr("pr.goesAfter", { list: pr.after.join(", ") }) : ""}
          </p>

          <div className={"glass-strong mt-7 rounded-3xl p-5"}>
            <p className={cx("text-[14px] font-semibold", pr.state === "decide" && "text-amber")}>
              {tr(ready ? "pr.merge" : pr.state === "decide" ? "pr.decide" : pr.state === "queued" ? "pr.queued" : "pr.notYet")}
            </p>
            <p className="mt-1 text-[13.5px] leading-relaxed text-fg-2">
              {ready ? tr(human ? "pr.mergeHuman" : "pr.mergeBot") : pr.why}
            </p>
            {pr.state === "queued" && firstUp.length ? (
              // what holds it is his own Merge of another PR: that button lives here too, not one screen away
              <div className="mt-3 space-y-2">
                {firstUp.map((o) => (
                  <div key={o.url} className="flex items-center gap-3 rounded-xl bg-hover px-3 py-2.5">
                    <GitMerge size={14} strokeWidth={1.9} className={cx("shrink-0", o.approval === "human" ? "text-[#1f9d55]" : "text-[#e0700f]")} />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[13px] text-fg">
                        <span className="font-medium">{o.label}</span> · <span className="first-letter:uppercase">{o.title}</span>
                      </p>
                      <p className="truncate text-[12px] text-fg-3">{tr("pr.firstThis", { why: o.why.charAt(0).toLowerCase() + o.why.slice(1) })}</p>
                    </div>
                    <PrActions pr={o} />
                  </div>
                ))}
              </div>
            ) : null}
            {!ready && related.length ? (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {related.map((o) => (
                  <a
                    key={o.label}
                    href={o.mine ? href({ page: "pr", repo: o.repo, n: o.n }) : githubPr(o.repo, o.n)}
                    className="inline-flex h-[22px] items-center gap-1.5 rounded-full bg-blue-soft px-2.5 text-[12px] text-blue hover:underline"
                  >
                    {o.label}
                    {o.why ? <span className="text-fg-3">· {o.why}</span> : null}
                  </a>
                ))}
              </div>
            ) : null}
            <div className="mt-4">
              {ready ? <PrActions pr={pr} /> : (
                <a href={pr.url} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center gap-1 rounded-lg px-2.5 text-[13px] text-fg shadow-[0_0_0_0.5px_var(--line-strong)] hover:bg-hover">
                  {tr("pr.viewGithub")}
                </a>
              )}
            </div>
          </div>

          <Repro pr={pr} />

          {t ? (
            <a href={href({ page: "thread", id: t.id })} className="mt-6 block rounded-2xl p-4 shadow-[0_0_0_0.5px_var(--line-strong)] transition hover:bg-hover">
              <p className="flex items-center gap-2 text-[12px] font-medium text-fg-3">
                <Glyph phase={t.phase} size={6} /> {tr("pr.case", { ago: ago(t.updated) })}
              </p>
              <p className="mt-1.5 text-[14px] font-medium text-fg">{t.headline}</p>
              {t.origin ? <p className="mt-0.5 text-[13px] text-fg-3">{t.origin}.</p> : null}
              {t.bullets.length ? (
                <ul className="mt-2 space-y-1">
                  {t.bullets.slice(0, 3).map((b) => (
                    <li key={b} className="text-[13px] text-fg-2">· {b}</li>
                  ))}
                </ul>
              ) : null}
              <p className="mt-3 inline-flex items-center gap-1.5 text-[13px] text-fg-2">
                <MessageSquare size={13} /> {tr("pr.fullStory")}
              </p>
            </a>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** Did this PR prove itself against the bug? Green: the reproduction test failed without the fix and passes with it.
    Red: anything less, said plainly. */
function Repro({ pr }: { pr: OwnPr }) {
  const r = pr.repro;
  const ok = !!r?.e2e;
  const when = (ts: number | null) => (ts ? `${dayLabel(ts).toLowerCase()} ${clock(ts)}` : "");
  const title = tr(
    ok ? "pr.repro.ok" : !r ? "pr.repro.none" : r.verdict === "reproduced" ? "pr.repro.notGreen" : r.verdict === "observed" ? "pr.repro.observed" : "pr.repro.failed",
  );
  const body = ok
    ? tr("pr.repro.okBody", { red: when(r!.red_at), green: when(r!.green_at) })
    : !r
      ? tr("pr.repro.noneBody")
      : r.verdict === "reproduced"
        ? r.green_fail
          ? tr(r.green_on === "main+PR" ? "pr.repro.stillFailsMain" : "pr.repro.stillFails", { red: when(r.red_at) })
          : tr("pr.repro.noGreen", { red: when(r.red_at) })
        : r.rejected || r.question || r.why || tr("pr.repro.seeCase");
  return (
    <div
      className={cx(
        "mt-4 rounded-3xl p-5",
        ok ? "bg-green-soft shadow-[0_0_0_0.5px_color-mix(in_srgb,var(--green)_35%,transparent)]" : "bg-red-soft shadow-[0_0_0_0.5px_color-mix(in_srgb,var(--red)_30%,transparent)]",
      )}
    >
      <p className={cx("flex items-center gap-2 text-[14px] font-semibold", ok ? "text-green" : "text-red")}>
        {ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />} {title}
      </p>
      <p className="mt-1.5 text-[13.5px] leading-relaxed text-fg">{body}</p>
      {r?.files.length ? (
        <div className="mt-3 space-y-1">
          {r.files.map((f) => (
            <p key={f} className="truncate font-mono text-[11.5px] text-fg-2" title={f}>
              {f}
            </p>
          ))}
        </div>
      ) : null}
      {r?.failure ? (
        <p className="mt-2 line-clamp-3 text-[12.5px] text-fg-2">
          <span className="text-fg-3">{tr("pr.repro.failure")}</span>
          {r.failure}
        </p>
      ) : null}
      {r?.green_fail ? (
        <pre className="mt-2 max-h-40 overflow-auto rounded-xl bg-panel/60 p-2.5 font-mono text-[11px] whitespace-pre-wrap text-fg-2">{r.green_fail}</pre>
      ) : null}
      {r?.backfill && r.verdict === "reproduced" ? (
        <p className="mt-2 text-[12px] text-fg-3">
          {tr(r.green_on === "PR" ? "pr.repro.backfillPr" : "pr.repro.backfillMain", { branch: r.branch ?? "" })}
        </p>
      ) : null}
      {ok && r?.green_head && pr.head && r.green_head !== pr.head ? (
        <p className="mt-2 text-[12px] text-fg-3">{tr("pr.repro.stale")}</p>
      ) : null}
    </div>
  );
}

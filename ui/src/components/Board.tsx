import { motion } from "motion/react";
import { ArrowRight, CheckCircle2, ChevronRight, CircleDashed, GitMerge, Layers } from "lucide-react";
import { useState } from "react";
import { PR_GROUPS, byOpened, sendChat, type Board as B, type OwnPr, type Thread } from "../api";
import { listOf, t as tr, tn } from "../i18n"; // tr: in this file t is a thread
import { ago, cx } from "../lib";
import { go, href } from "../router";
import { firstName, seesProduction } from "../setup";
import { Composer } from "./Composer";
import { AppIcon, Card, CiDot, Empty, Glyph, Mark, PrLink, SectionHead, TicketLink } from "./ui";

/** The main screen: what needs him first, then his PRs, then what the agent is doing and what it did and why. */
export function Board({ board, threads }: { board: B | null; threads: Thread[] }) {
  if (!board) return <Loading />;
  const prs = board.prs;
  const merge = prs.filter((p) => p.state === "merge");
  const review = prs.filter((p) => p.state === "review");
  const forYou = prs.filter((p) => ["decide", "blocked", "draft"].includes(p.state));
  const running = threads.filter((t) => t.phase === "running");
  const needs = threads.filter((t) => t.phase === "you").sort((a, b) => b.created - a.created);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-[1120px] px-8 pt-[12vh] pb-20">
        <header className="rise mx-auto max-w-[48rem] text-center">
          <div className="mb-6 flex justify-center">
            <AppIcon size={60} busy={running.length > 0} intro />
          </div>
          <h1 className="text-[32px] leading-[2.375rem] font-normal tracking-[-0.025em]">{greet(board.greeting)}</h1>
          <p className="mt-2 text-[14px] text-fg-2">{summary(needs.length, merge.length, forYou.length, review.length, running.length)}</p>
          <div className="mt-8 text-left">
            <Composer
              size="hero"
              placeholder={tr("board.ask")}
              hint={tr(seesProduction() ? "board.askHint" : "board.askHintNoProd")}
              onSend={async (t) => {
                const conv = await sendChat(t);
                go({ page: "chat", conv });
              }}
            />
          </div>
          <Stats board={board} />
        </header>

        <div className="rise rise-2 mt-14 grid grid-cols-1 gap-x-8 gap-y-12 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
          <div className="space-y-12">
            <ReadyToMerge prs={merge} />
            <Needs needs={needs} />
            <Prs prs={prs} />
          </div>
          <div className="space-y-12">
            <Running running={running} waiting={threads.filter((t) => t.phase === "waiting")} />
            <Shipped shipped={board.shipped} />
          </div>
        </div>
      </div>
    </div>
  );
}

/** "Good morning, Ada": the worker writes the greeting and setup knows the name. With no name the greeting stands alone. */
function greet(greeting: string): string {
  const name = firstName();
  return name ? `${greeting}, ${name}` : greeting;
}

function summary(needs: number, merge: number, forYou: number, review: number, running: number): string {
  const parts: string[] = [];
  if (merge) parts.push(tn("board.sum.merge", merge));
  if (forYou) parts.push(tn("board.sum.decide", forYou));
  if (needs) parts.push(tn("board.sum.asks", needs));
  if (review) parts.push(tn("board.sum.review", review));
  const head = parts.length ? tr("board.sum.head", { list: listOf(parts) }) : tr("board.sum.none");
  return head + (running ? ` ${tn("board.sum.running", running)}` : "");
}

function Stats({ board }: { board: B }) {
  const s = board.stats;
  const items = [
    { k: tn("board.stat.open", s.open), v: s.open },
    { k: tn("board.stat.prs", s.prs_week), v: s.prs_week },
    { k: tr("board.stat.done"), v: s.done_week },
    { k: tr("board.stat.cost"), v: tr("money", { v: s.cost_today.toFixed(0) }) },
  ];
  return (
    <div className="mt-4 flex flex-wrap items-center justify-center gap-x-5 gap-y-1 text-[13px] text-fg-3">
      {items.map((i) => (
        <span key={i.k}>
          <span className="text-fg-2 tabular-nums">{i.v}</span> {i.k}
        </span>
      ))}
    </div>
  );
}

function ReadyToMerge({ prs }: { prs: OwnPr[] }) {
  if (!prs.length) return null;
  const rows = [...prs].sort((a, b) => (b.created ?? "").localeCompare(a.created ?? ""));
  return (
    <section>
      <SectionHead
        title={tr("board.ready")}
        count={prs.length}
        right={
          <span className="flex items-center gap-3 text-[12px] text-fg-3">
            <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-[#1f9d55]" /> {tr("board.ready.human")}</span>
            <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-[#e0700f]" /> {tr("board.ready.bot")}</span>
          </span>
        }
      />
      <Card>
        {rows.map((p, i) => (
          <a
            key={p.url}
            href={href({ page: "pr", repo: p.repo, n: p.number })}
            className={cx("group flex items-center gap-3 px-4 py-3 transition hover:bg-hover", i > 0 && "border-t-[0.5px] border-line-strong")}
          >
            <GitMerge size={15} strokeWidth={1.9} className={cx("shrink-0", p.approval === "human" ? "text-[#1f9d55]" : "text-[#e0700f]")} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[14px] leading-5 text-fg first-letter:uppercase">{p.title}</p>
              <p className="truncate text-[12px] text-fg-3">
                {p.label}
                {p.ticket ? ` · ${p.ticket}` : ""} · {p.why}
              </p>
            </div>
            <ChevronRight size={15} className="shrink-0 text-fg-3 opacity-0 transition group-hover:opacity-100" />
          </a>
        ))}
      </Card>
    </section>
  );
}

function Needs({ needs }: { needs: Thread[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? needs : needs.slice(0, 7);
  return (
    <section>
      <SectionHead title={tr("board.needs")} count={needs.length} />
      <Card>
        {shown.length ? (
          shown.map((t, i) => <NeedRow key={t.id} t={t} first={i === 0} />)
        ) : (
          <Empty>{tr("board.needs.empty")}</Empty>
        )}
        {needs.length > 7 ? (
          <button
            onClick={() => setAll((a) => !a)}
            className="flex h-10 w-full items-center justify-center gap-1 border-t-[0.5px] border-line-strong text-[13px] text-fg-2 transition hover:bg-hover hover:text-fg"
          >
            {all ? tr("board.showLess") : tr("board.showAll", { n: needs.length })}
          </button>
        ) : null}
      </Card>
    </section>
  );
}

function NeedRow({ t, first }: { t: Thread; first: boolean }) {
  const old = Date.now() / 1000 - t.updated > 3 * 86400;
  return (
    <a
      href={href({ page: "thread", id: t.id })}
      className={cx("group flex gap-3 px-4 py-3 transition hover:bg-hover", !first && "border-t-[0.5px] border-line-strong")}
    >
      <div className="grid h-5 w-4 shrink-0 place-items-center">
        <Glyph phase={t.phase} size={7} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start gap-2">
          <p className="min-w-0 flex-1 text-[14px] leading-5 text-fg">{t.headline}</p>
          <span className={cx("shrink-0 text-[13px] tabular-nums", old ? "text-amber" : "text-fg-3")}>{ago(t.updated)}</span>
        </div>
        {t.origin ? <p className="mt-0.5 truncate text-[13px] text-fg-3">{t.origin}</p> : null}
        {t.need ? (
          <p className="mt-1 flex gap-1.5 text-[13px] leading-[1.43] text-fg-2">
            <ArrowRight size={13} className="mt-[3px] shrink-0 text-amber" />
            <span className="line-clamp-2">{t.need}</span>
          </p>
        ) : null}
        {t.ticket || t.prs.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {t.ticket ? <TicketLink id={t.ticket} /> : null}
            {t.prs.slice(0, 3).map((p) => (
              <PrLink key={p.url} pr={p} />
            ))}
          </div>
        ) : null}
      </div>
      <ChevronRight size={15} className="mt-0.5 shrink-0 text-fg-3 opacity-0 transition group-hover:opacity-100" />
    </a>
  );
}

/** Every PR that is not ready to merge, grouped by what it needs, all visible: no tabs, nothing cut. */
function Prs({ prs }: { prs: OwnPr[] }) {
  const groups = PR_GROUPS.map((g) => ({ ...g, prs: prs.filter((p) => g.states.includes(p.state)).sort(byOpened) })).filter((g) => g.prs.length);
  const n = groups.reduce((k, g) => k + g.prs.length, 0);
  if (!n) return null;
  return (
    <section>
      <SectionHead title={tr("board.otherPrs")} count={n} right={<span className="text-[12px] text-fg-3">{tr("board.newestFirst")}</span>} />
      <div className="space-y-5">
        {groups.map((g) => (
          <div key={g.k}>
            <p className={cx("mb-2 px-1 text-[13px]", g.k === "you" ? "font-medium text-amber" : "text-fg-3")}>
              {tr(`prGroup.${g.k}.label`)} · {g.prs.length} <span className="text-fg-3 font-normal">· {tr(`prGroup.${g.k}.hint`)}</span>
            </p>
            <Card>
              {g.prs.map((p, i) => (
                <PrRow key={p.url} p={p} first={i === 0} />
              ))}
            </Card>
          </div>
        ))}
      </div>
    </section>
  );
}

function PrRow({ p, first }: { p: OwnPr; first: boolean }) {
  const days = p.created ? Math.floor((Date.now() - Date.parse(p.created)) / 86400000) : 0;
  const you = ["blocked", "decide", "draft"].includes(p.state);
  const Icon = p.state === "merge" ? GitMerge : you ? CircleDashed : Layers;
  return (
    <a href={href({ page: "pr", repo: p.repo, n: p.number })} className={cx("flex h-12 items-center gap-3 px-4 transition hover:bg-hover", !first && "border-t-[0.5px] border-line-strong")}>
      <Icon
        size={15}
        strokeWidth={1.9}
        className={cx("shrink-0", p.state === "merge" ? "text-green" : you ? "text-amber" : "text-fg-3")}
      />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[14px] leading-5 text-fg first-letter:uppercase">{p.title}</p>
        <p className="truncate text-[12px] text-fg-3">
          {p.why}
          {/* the reason often names the PR it waits on already, in either language */}
          {p.after.length && !/después de|primero|after|first/i.test(p.why) ? ` · ${tr("board.after", { list: p.after.join(", ") })}` : ""}
        </p>
      </div>
      <span className={cx("w-12 shrink-0 text-right text-[13px] tabular-nums", days > 14 ? "text-amber" : "text-fg-3")}>
        {days ? tr("time.d", { n: days }) : tr("time.today")}
      </span>
      <span className="inline-flex h-[22px] shrink-0 items-center gap-1.5 rounded-full bg-blue-soft py-0.5 ps-2 pe-2.5 text-[12px] text-blue">
        {p.label}
        {p.ci ? <CiDot ci={p.ci} /> : null}
      </span>
    </a>
  );
}

function Running({ running, waiting }: { running: Thread[]; waiting: Thread[] }) {
  return (
    <section>
      <SectionHead title={tr("board.running")} count={running.length} right={<a href={href({ page: "threads" })} className="text-[13px] text-fg-3 hover:text-fg">{tr("board.viewThreads")}</a>} />
      <div className="space-y-2">
        {running.length ? (
          running.map((t) => (
            <motion.a
              layout
              key={t.id}
              href={href({ page: "thread", id: t.id })}
              className="glass lift flex gap-3 rounded-2xl p-3.5"
            >
              <span className="grid h-5 w-4 shrink-0 place-items-center">
                <Mark size={15} busy className="text-brand-ink" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-start gap-2">
                  <p className="min-w-0 flex-1 text-[14px] leading-5">{t.headline}</p>
                  <span className="shrink-0 text-[13px] text-fg-3 tabular-nums">{ago(t.updated)}</span>
                </div>
                <p className="mt-1 truncate text-[13px]">
                  <span className="shimmer">{t.last || t.stage}</span>
                </p>
              </div>
            </motion.a>
          ))
        ) : (
          <Card>
            <Empty>{tr("board.running.empty")}</Empty>
          </Card>
        )}
        {waiting.length ? (
          <p className="px-1 pt-1 text-[13px] text-fg-3">
            {tn("board.waiting", waiting.length)}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function Shipped({ shipped }: { shipped: Thread[] }) {
  const rows = [...shipped].sort((a, b) => b.updated - a.updated).slice(0, 12);
  return (
    <section>
      <SectionHead title={tr("board.shipped")} count={shipped.length} />
      <div className="relative pl-5">
        <div className="absolute top-2 bottom-2 left-[5.5px] w-[0.5px] bg-line-strong" />
        {rows.length ? (
          rows.map((t) => (
            <a key={t.id} href={href({ page: "thread", id: t.id })} className="group relative block py-2.5">
              <CheckCircle2 size={11} className="absolute top-[14px] -left-5 rounded-full bg-bg text-green" />
              <p className="text-[14px] leading-5 text-fg">{t.headline}</p>
              <p className="mt-0.5 line-clamp-2 text-[13px] text-fg-3">{why(t)}</p>
            </a>
          ))
        ) : (
          <p className="py-3 text-[13px] text-fg-3">{tr("board.shipped.empty")}</p>
        )}
      </div>
    </section>
  );
}

function why(t: Thread): string {
  const o = t.origin ? t.origin : "";
  const note = t.last.replace(/^(?:Hecho|Done):?\s*/, ""); // the worker's closing word, in either language
  return [o, note && !/^(?:Hecho|Done)$/.test(note) ? note : ""].filter(Boolean).join(" · ");
}

function Loading() {
  return (
    <div className="grid h-full place-items-center">
      <div className="flex flex-col items-center gap-4 text-[14px]">
        <Mark size={36} busy className="text-brand-ink" />
        <span className="shimmer">{tr("board.loading")}</span>
      </div>
    </div>
  );
}

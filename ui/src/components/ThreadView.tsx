import { motion } from "motion/react";
import { ArrowLeft, ArrowUpRight, Check, GitMerge, ChevronDown, Copy, Fingerprint, FlaskConical, MessageSquare, PenLine, RotateCw, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { caseAction, decideAsk, usePoll, type Ask, type AskState, type CaseActionState, type ChatSummary, type Event, type NextStep, type OwnPr, type Thread, type ThreadDetail } from "../api";
import { t as tr, useLang, type Key } from "../i18n"; // tr: in this file t is a thread
import { ago, clock, cx, dayLabel } from "../lib";
import { href } from "../router";
import { ChatThread } from "./Chat";
import { PrActions } from "./PrActions";
import { ExtLink, Glyph, Mark, Md, PrLink, Spinner, Tag, TicketLink } from "./ui";

const PHASE_TONE = { you: "amber", running: "accent", waiting: "neutral", done: "green", closed: "neutral" } as const;

/** One case as a story: what it is, what it needs, what the agent did and why, and a conversation about it. */
export function ThreadView({ id, chats, compact, onClose, own, ownPrs, peek }: { id: number; chats: ChatSummary[]; compact?: boolean; onClose?: () => void; own?: Set<string>; ownPrs?: OwnPr[]; peek?: Thread }) {
  const { data: t, error } = usePoll<ThreadDetail>(`/api/thread/${id}`, 5000);
  const caseChat = chats.find((c) => c.item === id);
  const [conv, setConv] = useState<string | null>(caseChat?.conv ?? null);
  const [tab, setTab] = useState<"story" | "chat">("story");
  const activeConv = conv ?? caseChat?.conv ?? null;

  const nav = compact ? (
    <button onClick={onClose} aria-label={tr("common.close")} className="grid size-7 place-items-center rounded-md text-fg-3 hover:bg-hover hover:text-fg">
      <X size={15} />
    </button>
  ) : (
    <a href={href({ page: "board" })} className="grid size-7 place-items-center rounded-md text-fg-3 hover:bg-hover hover:text-fg" aria-label={tr("common.back")}>
      <ArrowLeft size={15} />
    </a>
  );

  // the list already knows the case: its header shows at once while the rest loads (a click never opens a blank page)
  if (!t && peek && !error)
    return (
      <div className="flex h-full min-w-0 flex-col">
        <div data-drag className="flex h-[46px] shrink-0 items-center gap-2 border-b-[0.5px] border-line-strong px-3">
          {nav}
          <span className="truncate text-[13px] text-fg-2">{peek.headline}</span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className={cx("mx-auto flex w-full gap-10", compact ? "flex-col px-5 py-6" : "max-w-[1120px] px-8 py-10")}>
            <main className="min-w-0 flex-1">
              <Header t={peek} own={own} />
              <p className="shimmer mt-8 text-[13px] text-fg-3">{tr("case.loadingDetail")}</p>
            </main>
          </div>
        </div>
      </div>
    );

  if (!t)
    return (
      <div className="grid h-full place-items-center text-[13px] text-fg-3">
        {/* only a 404 means the case is gone; anything else is Clea not answering */}
        {!error ? <span className="shimmer">{tr("case.loading")}</span> : error.startsWith("404") ? tr("case.notFound") : tr("case.down")}
      </div>
    );

  return (
    <div className="flex h-full min-w-0 flex-col">
      <div data-drag className="flex h-[46px] shrink-0 items-center gap-2 border-b-[0.5px] border-line-strong px-3">
        {nav}
        <span className="truncate text-[13px] text-fg-2">{t.headline}</span>
        <div className="ml-auto flex items-center gap-1 rounded-lg bg-hover p-0.5">
          <TabBtn on={tab === "story"} onClick={() => setTab("story")}>
            {tr("case.story")}
          </TabBtn>
          <TabBtn on={tab === "chat"} onClick={() => setTab("chat")}>
            <MessageSquare size={12} /> {tr("case.chat")}
          </TabBtn>
        </div>
        {compact ? (
          <a href={href({ page: "thread", id })} className="grid size-7 place-items-center rounded-md text-fg-3 hover:bg-hover hover:text-fg" aria-label={tr("common.open")}>
            <ArrowUpRight size={15} />
          </a>
        ) : null}
      </div>

      {tab === "chat" ? (
        <ChatThread
          conv={activeConv}
          item={id}
          compact={compact}
          onConv={setConv}
          emptyTitle={tr("case.chatEmpty", { headline: t.headline })}
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className={cx("mx-auto flex w-full gap-10", compact ? "flex-col px-5 py-6" : "max-w-[1120px] px-8 py-10")}>
            <main className="min-w-0 flex-1">
              <Header t={t} own={own} />
              {t.asks.map((a) => (
                <AskCard key={a.id} a={a} />
              ))}
              {t.next ? <Next key={`${t.id}:${t.next.kind}:${t.status}`} id={t.id} n={t.next} onReply={() => setTab("chat")} /> : null}
              <CasePrs t={t} prs={ownPrs} />
              {t.bullets.length ? (
                <ul className="mt-6 space-y-1.5">
                  {t.bullets.map((b) => (
                    <li key={b} className="flex gap-2.5 text-[14px] leading-relaxed text-fg">
                      <span className="mt-[9px] size-1 shrink-0 rounded-full bg-fg-3" />
                      {b}
                    </li>
                  ))}
                </ul>
              ) : null}
              <Why t={t} />
              <Timeline events={t.events} running={t.phase === "running"} stage={t.stage} />
              {!compact ? (
                <button
                  onClick={() => setTab("chat")}
                  className="glass lift mt-8 flex w-full items-center gap-3 rounded-[20px] px-4 py-3 text-left text-[14px] text-fg-3 hover:text-fg-2"
                >
                  <MessageSquare size={16} />
                  {tr(activeConv ? "case.continueChat" : "case.startChat")}
                </button>
              ) : null}
            </main>
            <Inspector t={t} compact={compact} />
          </div>
        </div>
      )}
    </div>
  );
}

function TabBtn({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cx("relative flex h-6 items-center gap-1.5 rounded-md px-2.5 text-[12px] transition", on ? "text-fg" : "text-fg-2 hover:text-fg")}
    >
      {on ? (
        <motion.span
          layoutId="case-tab"
          transition={{ type: "spring", duration: 0.3, bounce: 0.05 }}
          className="absolute inset-0 rounded-md bg-panel shadow-[var(--shadow),0_0_0_0.5px_var(--line)]"
        />
      ) : null}
      <span className="relative flex items-center gap-1.5">{children}</span>
    </button>
  );
}

function Header({ t, own }: { t: Thread | ThreadDetail; own?: Set<string> }) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5">
        <Tag tone={PHASE_TONE[t.phase]}>
          <Glyph phase={t.phase} size={6} /> {tr(`phase.${t.phase}`)}
        </Tag>
        <Tag>{t.kind}</Tag>
        {t.ticket ? <TicketLink id={t.ticket} /> : null}
        {t.prs.map((p) => {
          const m = p.url.match(/github\.com\/[\w.-]+\/([\w.-]+)\/pull\/(\d+)/);
          const mine = !!m && !!own?.has(p.url.replace(/\/$/, ""));
          return <PrLink key={p.url} pr={p} to={mine && m ? href({ page: "pr", repo: m[1], n: Number(m[2]) }) : undefined} />;
        })}
      </div>
      <h1 className="mt-4 text-[24px] leading-[1.75rem] font-semibold text-balance">{t.headline}</h1>
      {t.origin ? <p className="mt-2 text-[14px] text-fg-2">{t.origin}.</p> : null}
    </div>
  );
}

/** The case's open PRs with where each one stands, so a case never hides its PR. */
function CasePrs({ t, prs }: { t: ThreadDetail; prs?: OwnPr[] }) {
  const mine = t.prs.map((p) => prs?.find((o) => o.url.replace(/\/$/, "") === p.url.replace(/\/$/, ""))).filter((o): o is OwnPr => !!o);
  if (!mine.length) return null;
  return (
    <div className="mt-6 space-y-2">
      {mine.map((p) => {
        const you = ["decide", "blocked", "draft"].includes(p.state);
        return (
          <a
            key={p.url}
            href={href({ page: "pr", repo: p.repo, n: p.number })}
            className="glass lift flex items-center gap-3 rounded-2xl px-4 py-3"
          >
            <GitMerge size={15} strokeWidth={1.9} className={cx("shrink-0", p.state === "merge" ? (p.approval === "human" ? "text-[#1f9d55]" : "text-[#e0700f]") : you ? "text-amber" : "text-fg-3")} />
            <div className="min-w-0 flex-1">
              <p className="text-[12px] font-medium text-fg-3">
                {tr("case.pr", { label: p.label })}
                {p.state === "merge" ? tr("case.pr.ready") : you ? tr("case.pr.you") : ""}
              </p>
              <p className="mt-0.5 truncate text-[14px] text-fg first-letter:uppercase">{p.title}</p>
              <p className="mt-0.5 text-[13px] text-fg-2">{p.why}</p>
            </div>
            <ArrowUpRight size={15} className="shrink-0 text-fg-3" />
          </a>
        );
      })}
    </div>
  );
}

const PRIMARY = "btn-brand inline-flex h-8 items-center gap-1.5 rounded-xl px-3 text-[13px] font-medium";
const SECONDARY = "press inline-flex h-8 items-center gap-1.5 rounded-xl px-3 text-[13px] text-fg shadow-[0_0_0_0.5px_var(--line-strong)] transition hover:bg-hover";
const GHOST = "inline-flex h-8 items-center rounded-xl px-3 text-[13px] text-fg-2 transition hover:bg-hover hover:text-fg";

/** What happens next and who moves. When it is him, there is always something to press. An ask has its own card above; this one carries what the ask does not. */
function Next({ id, n, onReply }: { id: number; n: NextStep; onReply: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [res, setRes] = useState<CaseActionState | null>(null);
  const [writing, setWriting] = useState<"go" | "retry" | null>(null);
  const [text, setText] = useState("");
  const { data: st } = usePoll<CaseActionState>(busy === "approve_review" ? `/api/case/${id}/approve_review` : null, 1000);
  const live = busy === "approve_review" && st && st.state !== "open" ? st : res;
  const waiting = !!busy && (!live || live.state === "waiting");
  const done = live?.state === "done";
  const failed = live?.state === "error" || live?.state === "declined";

  if (n.kind === "ask" && !n.prs.length) return null;
  if (n.who === "others" || n.who === "clea") {
    return (
      <div className="mt-6 flex items-start gap-2.5 text-[13px] leading-relaxed">
        <span className="mt-[5px]">
          <Glyph phase={n.who === "clea" ? "running" : "waiting"} size={7} />
        </span>
        <p className="min-w-0 text-fg-2">
          <span className="text-fg">{n.title}</span>
          {n.detail ? ` · ${n.detail}` : ""}
        </p>
      </div>
    );
  }

  const run = async (action: "go" | "retry" | "drop" | "approve_review", body = "") => {
    setBusy(action);
    setRes(null);
    try {
      const r = await caseAction(id, action, body);
      setRes(r);
      if (r.state === "error") setBusy(null);
      else setWriting(null);
    } catch (e) {
      setBusy(null);
      setRes({ state: "error", error: String(e).replace(/^Error: /, "") });
    }
  };

  const you = n.who === "you";
  return (
    <div className="glass-strong mt-6 overflow-hidden rounded-3xl">
      {n.kind !== "ask" ? (
        <>
          <div className="flex items-center gap-2 px-4 pt-4 pb-1">
            <span className={cx("size-1.5 rounded-full", done ? "bg-green" : you ? "bg-amber" : "bg-fg-3")} />
            <p className="text-[14px] font-semibold text-fg">{n.title}</p>
            {you ? <span className="ml-auto text-[12px] text-amber">{tr("case.yourMove")}</span> : null}
          </div>
          {n.detail ? <Md className="px-4 pt-1 pb-2 text-[14px] text-fg-2">{n.detail}</Md> : null}
        </>
      ) : null}
      {n.prs.length ? (
        <div className={cx("space-y-2 px-4", n.kind === "ask" ? "pt-4 pb-4" : "pt-1 pb-2")}>
          {n.prs.map((p) => (
            <div key={p.url} className="flex items-center gap-3 rounded-xl bg-hover px-3 py-2.5">
              <GitMerge size={14} strokeWidth={1.9} className={cx("shrink-0", p.approval === "human" ? "text-[#1f9d55]" : "text-[#e0700f]")} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-[13px] text-fg">
                  <span className="font-medium">{p.label}</span> · <span className="first-letter:uppercase">{p.title}</span>
                </p>
                <p className="truncate text-[12px] text-fg-3">{p.why}</p>
              </div>
              <PrActions pr={p} />
            </div>
          ))}
        </div>
      ) : null}
      {writing && !waiting && !done ? (
        <div className="px-4 pt-1">
          <textarea
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={tr(writing === "go" ? "case.goWith.placeholder" : "case.retryWith.placeholder")}
            className="min-h-[72px] w-full resize-none rounded-xl bg-hover px-3 py-2.5 text-[14px] text-fg outline-none placeholder:text-fg-3"
          />
        </div>
      ) : null}
      {n.actions.length ? (
        <div className="flex flex-wrap items-center gap-2 px-4 pt-2 pb-4">
          {done ? (
            <span className="inline-flex h-8 items-center gap-1.5 text-[13px] text-green">
              <Check size={14} /> {tr(busy === "approve_review" ? "case.approved" : busy === "drop" ? "common.dismissed" : "case.goDone")}
            </span>
          ) : waiting ? (
            <span className="inline-flex h-8 items-center gap-2 text-[13px]">
              <Spinner size={13} />
              <span className="shimmer">{tr(busy === "approve_review" ? "case.touchApprove" : "common.applying")}</span>
            </span>
          ) : writing ? (
            <>
              <button disabled={!text.trim()} onClick={() => run(writing, text)} className={PRIMARY}>
                {tr(writing === "go" ? "case.sendAndGo" : "case.retryWithThis")}
              </button>
              <button onClick={() => setWriting(null)} className={GHOST}>
                {tr("common.cancel")}
              </button>
            </>
          ) : (
            <>
              {n.actions.includes("go") ? (
                <button onClick={() => run("go")} className={PRIMARY}>
                  <Check size={14} /> {tr("case.go")}
                </button>
              ) : null}
              {n.actions.includes("retry") ? (
                <button onClick={() => run("retry")} className={PRIMARY}>
                  <RotateCw size={13} /> {tr("case.retry")}
                </button>
              ) : null}
              {n.actions.includes("approve_review") ? (
                <button onClick={() => run("approve_review")} className={PRIMARY}>
                  <Fingerprint size={14} /> {tr("case.approveGithub")}
                </button>
              ) : null}
              {n.actions.includes("go_with") ? (
                <button onClick={() => setWriting("go")} className={SECONDARY}>
                  <PenLine size={13} /> {tr("case.goWith")}
                </button>
              ) : null}
              {n.actions.includes("retry_with") ? (
                <button onClick={() => setWriting("retry")} className={SECONDARY}>
                  <PenLine size={13} /> {tr("case.retryWith")}
                </button>
              ) : null}
              {n.actions.includes("reply") ? (
                <button onClick={onReply} className={SECONDARY}>
                  <MessageSquare size={13} /> {tr("case.ask")}
                </button>
              ) : null}
              {n.actions.includes("drop") ? (
                <button onClick={() => run("drop")} className={GHOST}>
                  {tr("common.dismiss")}
                </button>
              ) : null}
            </>
          )}
          {failed ? <p className="w-full text-[13px] text-red">{live?.error}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

function AskCard({ a }: { a: Ask }) {
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [revising, setRevising] = useState(false);
  const [change, setChange] = useState("");
  const { data: st } = usePoll<AskState>(busy ? `/api/ask/${a.id}` : null, 1000);
  const state = busy ? st?.state ?? "waiting" : "open";
  const finished = state === "done";
  const failed = state === "declined" || state === "error";

  const go = async (d: "approve" | "reject" | "revise") => {
    setBusy(d);
    try {
      await decideAsk(a.id, d, d === "revise" ? change : "");
    } catch (e) {
      setBusy(null);
      alert(String(e));
    }
  };

  return (
    <div className="glass-strong mt-6 overflow-hidden rounded-3xl">
      <div className="flex items-center gap-2 px-4 pt-4 pb-1">
        <span className={cx("size-1.5 rounded-full", finished ? "bg-green" : "bg-amber")} />
        <p className="text-[14px] font-semibold text-fg">{a.what}</p>
        <span className="ml-auto text-[12px] text-fg-3">{a.expired ? tr("ask.expired") : ago(a.at)}</span>
      </div>
      {a.text ? (
        <div data-selectable className="mx-4 my-2 max-h-[360px] overflow-y-auto rounded-xl bg-hover px-3.5 py-3">
          <Md className="text-[14px]">{a.text}</Md>
        </div>
      ) : null}
      {a.comments.length ? (
        <div className="space-y-2 px-4 py-2">
          {a.comments.map((c, i) => (
            <div key={i} className="rounded-xl bg-hover px-3 py-2">
              <p className="font-mono text-[11px] text-fg-3">
                {c.path}
                {c.line ? `:${c.line}` : ""}
              </p>
              <Md className="mt-1 text-[14px]">{c.body}</Md>
            </div>
          ))}
        </div>
      ) : null}
      {revising && !busy ? (
        <div className="px-4 pt-1">
          <textarea
            autoFocus
            value={change}
            onChange={(e) => setChange(e.target.value)}
            placeholder={tr("ask.revise.placeholder")}
            className="min-h-[72px] w-full resize-none rounded-xl bg-hover px-3 py-2.5 text-[14px] text-fg outline-none placeholder:text-fg-3"
          />
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2 px-4 pt-2 pb-4">
        {finished ? (
          <span className="inline-flex h-8 items-center gap-1.5 text-[13px] text-green">
            <Check size={14} /> {tr(busy === "approve" ? "ask.sent" : busy === "reject" ? "common.dismissed" : "ask.revised")}
          </span>
        ) : busy && !failed ? (
          <span className="inline-flex h-8 items-center gap-2 text-[13px]">
            <Spinner size={13} />
            <span className="shimmer">{tr(busy === "approve" ? "ask.touchSend" : "common.applying")}</span>
          </span>
        ) : revising ? (
          <>
            <button
              disabled={!change.trim()}
              onClick={() => go("revise")}
              className={PRIMARY}
            >
              {tr("ask.requestChange")}
            </button>
            <button onClick={() => setRevising(false)} className="inline-flex h-8 items-center rounded-xl px-3 text-[13px] text-fg-2 hover:bg-hover">
              {tr("common.cancel")}
            </button>
          </>
        ) : (
          <>
            <button
              onClick={() => go("approve")}
              className={PRIMARY}
            >
              <Fingerprint size={14} /> {tr("ask.approve")}
            </button>
            <button
              onClick={() => setRevising(true)}
              className={SECONDARY}
            >
              <PenLine size={13} /> {tr("ask.requestChanges")}
            </button>
            <button
              onClick={() => go("reject")}
              className="inline-flex h-8 items-center rounded-xl px-3 text-[13px] text-fg-2 transition hover:bg-hover hover:text-fg"
            >
              {tr("common.dismiss")}
            </button>
            {a.text ? (
              <button
                onClick={() =>
                  navigator.clipboard.writeText(a.text).then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1200);
                  })
                }
                className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-xl px-2.5 text-[13px] text-fg-3 transition hover:bg-hover hover:text-fg"
              >
                {copied ? <Check size={13} /> : <Copy size={13} />} {tr(copied ? "common.copied" : "common.copy")}
              </button>
            ) : null}
          </>
        )}
        {failed ? <p className="w-full text-[13px] text-red">{st?.error}</p> : null}
      </div>
    </div>
  );
}

function Why({ t }: { t: ThreadDetail }) {
  if (!t.analysis) {
    return t.technical ? <p className="shimmer mt-8 text-[13px] text-fg-3">{tr("case.writingAnalysis")}</p> : null;
  }
  return (
    <section className="mt-8">
      <div className="flex items-center gap-2">
        <h2 className="text-[13px] font-medium text-fg-2">{tr("case.analysis")}</h2>
        {t.cause && t.cause !== "no aplica" ? <Tag tone={t.cause === "probado" ? "green" : "neutral"}>{CAUSE[t.cause] ? tr(CAUSE[t.cause]) : tr("case.cause.other", { cause: t.cause })}</Tag> : null}
      </div>
      <Md className="mt-3 text-[14px] leading-relaxed text-fg">{t.analysis}</Md>
    </section>
  );
}

/** The worker names how sure the cause is in its own words; these are the ones the UI knows how to say. */
const CAUSE: Record<string, Key> = { probado: "case.cause.proven", probable: "case.cause.probable", "sin confirmar": "case.cause.unconfirmed" };

function Timeline({ events, running, stage }: { events: Event[]; running: boolean; stage: string }) {
  const lang = useLang(); // the day labels are text: regroup when the language changes
  const groups = useMemo(() => {
    const out: { day: string; items: Event[] }[] = [];
    for (const e of events) {
      const d = dayLabel(e.at);
      if (!out.length || out[out.length - 1].day !== d) out.push({ day: d, items: [] });
      out[out.length - 1].items.push(e);
    }
    return out;
  }, [events, lang]);
  return (
    <section className="mt-10">
      <h2 className="mb-4 text-[13px] font-medium text-fg-2">{tr("case.timeline")}</h2>
      <div className="space-y-6">
        {groups.map((g) => (
          <div key={g.day}>
            <p className="mb-2 text-[12px] font-medium text-fg-3">{g.day}</p>
            <ol className="relative">
              {g.items.map((e, i) => (
                <li key={i} className="relative flex gap-3.5 pb-3.5 last:pb-0">
                  {i < g.items.length - 1 ? <span className="absolute top-[15px] bottom-0 left-[3.5px] w-px bg-line" /> : null}
                  <span className={cx("relative mt-[7px] size-2 shrink-0 rounded-full border-[1.5px]", dotColor(e.stage))} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      <p className="text-[14px] font-medium text-fg">{e.title}</p>
                      <span className="ml-auto shrink-0 font-mono text-[11px] text-fg-3 tabular-nums">{clock(e.at)}</span>
                    </div>
                    {e.note ? <p className="mt-0.5 text-[13px] leading-relaxed break-words text-fg-2">{e.note}</p> : null}
                  </div>
                </li>
              ))}
            </ol>
          </div>
        ))}
        {running ? (
          <div className="flex items-center gap-3 pl-0.5">
            <Mark size={14} busy className="text-brand-ink" />
            <span className="shimmer text-[14px]">{stage}</span>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function dotColor(stage: string): string {
  if (stage === "done") return "border-green bg-green";
  if (stage === "dropped" || stage === "failed" || stage === "expired") return "border-fg-3 bg-transparent";
  if (stage === "blocked") return "border-red bg-red-soft";
  if (stage.startsWith("awaiting")) return "border-amber bg-amber-soft";
  return "border-line-strong bg-panel";
}

function Inspector({ t, compact }: { t: ThreadDetail; compact?: boolean }) {
  const [src, setSrc] = useState(false);
  const [rep, setRep] = useState(false);
  const [tech, setTech] = useState(false);
  return (
    <aside className={cx("shrink-0 space-y-6", compact ? "w-full border-t border-line pt-6" : "w-[300px]")}>
      <Prop label={tr("case.status")}>
        <span className="text-fg">{t.stage}</span>
        <span className="text-fg-3"> · {ago(t.updated)}</span>
      </Prop>
      {t.source.link || t.source.author ? (
        <Prop label={tr("case.source")}>
          {t.source.author ? <span className="text-fg">{t.source.author}</span> : null}
          {t.source.link ? (
            <div className="mt-1">
              <ExtLink href={t.source.link}>{tr("case.sourceThread")}</ExtLink>
            </div>
          ) : null}
        </Prop>
      ) : null}
      {t.repro ? (
        <Prop label={tr("case.repro")}>
          <div className="flex items-center gap-2">
            <FlaskConical size={13} className={t.repro.green ? "text-green" : "text-fg-3"} />
            <span className="text-fg">
              {t.repro.green ? tr("case.repro.green") : t.repro.verdict === "reproduced" ? tr("case.repro.reproduced") : (t.repro.verdict ?? tr("case.repro.pending"))}
            </span>
          </div>
          {t.repro.files.map((f) => (
            <p key={f} className="mt-1 truncate font-mono text-[11px] text-fg-3" title={f}>
              {f}
            </p>
          ))}
        </Prop>
      ) : null}
      {t.caveats.length ? (
        <Prop label={tr("case.caveats")}>
          <ul className="space-y-1">
            {t.caveats.map((c) => (
              <li key={c} className="text-fg-2">
                {c}
              </li>
            ))}
          </ul>
        </Prop>
      ) : null}
      <Prop label={tr("case.cost")}>
        <span className="font-mono text-fg tabular-nums">{tr("money", { v: t.cost.toFixed(2) })}</span>
      </Prop>
      <Prop label={tr("case.opened")}>
        <span className="text-fg-2">
          {dayLabel(t.created)}, {clock(t.created)}
        </span>
      </Prop>
      {t.source.text ? (
        <Collapsible label={tr("case.sourceText")} open={src} onToggle={() => setSrc((s) => !s)}>
          <Md className="text-[13px] text-fg-2">{t.source.text}</Md>
        </Collapsible>
      ) : null}
      {t.technical ? (
        <Collapsible label={tr("case.technical")} open={tech} onToggle={() => setTech((s) => !s)}>
          <div className="space-y-3">
            {t.technical.problem ? <Md className="text-[13px] text-fg-2">{t.technical.problem}</Md> : null}
            {t.technical.cause ? (
              <div>
                <p className="mb-1 text-[12px] font-medium text-fg-3">{tr("case.technical.cause")}</p>
                <Md className="text-[13px] text-fg-2">{t.technical.cause}</Md>
              </div>
            ) : null}
            {t.technical.recommendation ? (
              <div>
                <p className="mb-1 text-[12px] font-medium text-fg-3">{tr("case.technical.recommendation")}</p>
                <Md className="text-[13px] text-fg-2">{t.technical.recommendation}</Md>
              </div>
            ) : null}
          </div>
        </Collapsible>
      ) : null}
      {t.report || t.detail ? (
        <Collapsible label={tr("case.detail")} open={rep} onToggle={() => setRep((s) => !s)}>
          <Md className="text-[13px] text-fg-2">{t.detail || t.report}</Md>
        </Collapsible>
      ) : null}
      <p className="font-mono text-[11px] text-fg-3">{tr("case.number", { id: t.id })}</p>
    </aside>
  );
}

function Prop({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="text-[13px]">
      <p className="mb-1 text-[12px] font-medium text-fg-3">{label}</p>
      {children}
    </div>
  );
}

function Collapsible({ label, open, onToggle, children }: { label: string; open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <div className="border-t border-line pt-4">
      <button onClick={onToggle} className="flex w-full items-center text-[12px] font-medium text-fg-3 hover:text-fg-2">
        {label}
        <ChevronDown size={13} className={cx("ml-auto transition", open && "rotate-180")} />
      </button>
      {open ? <div className="mt-2 max-h-[420px] overflow-y-auto">{children}</div> : null}
    </div>
  );
}

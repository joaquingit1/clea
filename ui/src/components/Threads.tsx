import { AnimatePresence, motion } from "motion/react";
import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import type { ChatSummary, Phase, Thread } from "../api";
import { t as tr } from "../i18n"; // tr: in this file t is a thread
import { ago, cx } from "../lib";
import { ThreadView } from "./ThreadView";
import { Glyph, PrLink, Tag, TicketLink } from "./ui";

// one column per phase; its title and hint are threads.col.<phase> and threads.col.<phase>.hint
const COLS: Exclude<Phase, "closed">[] = ["you", "running", "waiting", "done"];

/** Every thread at once, by what it needs; click opens it beside the board, ⌘-click opens another next to it. */
export function Threads({ threads, chats }: { threads: Thread[]; chats: ChatSummary[] }) {
  const [q, setQ] = useState("");
  const [panes, setPanes] = useState<number[]>([]);
  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return threads;
    return threads.filter((t) =>
      [t.headline, t.origin, t.ticket ?? "", t.kind, ...t.prs.map((p) => p.label), String(t.id)].join(" ").toLowerCase().includes(s),
    );
  }, [threads, q]);

  const open = (id: number, add: boolean) =>
    setPanes((p) => {
      if (p.includes(id)) return p;
      if (add) return [...p, id].slice(-3);
      return p.length ? [...p.slice(0, -1), id] : [id];
    });

  return (
    <div className="flex h-full min-w-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <div data-drag className="flex h-14 shrink-0 items-center gap-4 px-6">
          <h1 className="text-[15px] font-semibold tracking-[-0.01em]">{tr("threads.title")}</h1>
          <span className="text-[13px] text-fg-3">{tr("threads.hint")}</span>
          <label className="glass ml-auto flex h-8 w-[260px] items-center gap-2 rounded-lg px-2.5 text-fg-3">
            <Search size={14} />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={tr("threads.filter")}
              className="w-full bg-transparent text-[13px] text-fg outline-none placeholder:text-fg-3"
            />
          </label>
        </div>
        <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto px-6 pb-6">
          {COLS.map((phase) => {
            const rows = shown
              .filter((t) => t.phase === phase)
              .sort((a, b) => b.updated - a.updated);
            return (
              <div key={phase} className={cx("glass flex min-w-[272px] flex-1 flex-col rounded-[20px]", panes.length ? "max-w-[300px]" : "")}>
                <div className="flex items-center gap-2 px-3.5 pt-3 pb-2">
                  <Glyph phase={phase} size={7} />
                  <p className="text-[13px] font-medium">{tr(`threads.col.${phase}`)}</p>
                  <span className="font-mono text-[11px] text-fg-3 tabular-nums">{rows.length}</span>
                  <span className="ml-auto truncate text-[11px] text-fg-3">{tr(`threads.col.${phase}.hint`)}</span>
                </div>
                <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2">
                  {rows.map((t) => (
                    <ThreadCard key={t.id} t={t} on={panes.includes(t.id)} onOpen={(add) => open(t.id, add)} />
                  ))}
                  {!rows.length ? <p className="px-2 py-4 text-center text-[13px] text-fg-3">{tr("threads.empty")}</p> : null}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <AnimatePresence initial={false}>
        {panes.map((id) => (
          <motion.div
            key={id}
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: panes.length > 1 ? 460 : 560, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.24, ease: [0.2, 0.7, 0.2, 1] }}
            className="h-full shrink-0 overflow-hidden border-l border-line bg-bg"
          >
            <div className="h-full" style={{ width: panes.length > 1 ? 460 : 560 }}>
              <ThreadView id={id} chats={chats} compact onClose={() => setPanes((p) => p.filter((x) => x !== id))} peek={threads.find((t) => t.id === id)} />
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

function ThreadCard({ t, on, onOpen }: { t: Thread; on: boolean; onOpen: (add: boolean) => void }) {
  return (
    <motion.button
      layout="position"
      onClick={(e) => onOpen(e.metaKey || e.ctrlKey)}
      className={cx(
        "glass-strong lift block w-full rounded-xl p-3 text-left",
        on && "ring-2 ring-accent",
      )}
    >
      <div className="flex items-center gap-1.5">
        <Tag tone={t.phase === "running" ? "accent" : t.phase === "you" ? "amber" : "neutral"}>{t.kind}</Tag>
        {t.asks ? <Tag tone="amber">{tr("threads.oks", { n: t.asks })}</Tag> : null}
        <span className="ml-auto text-[11px] text-fg-3">{ago(t.updated)}</span>
      </div>
      <p className="mt-2 line-clamp-3 text-[13px] leading-snug font-medium text-fg">{t.headline}</p>
      <p className={cx("mt-1.5 line-clamp-2 text-[12px] leading-snug", t.phase === "running" ? "shimmer" : "text-fg-3")}>
        {t.phase === "you" && t.need ? t.need : t.last || t.stage}
      </p>
      {t.ticket || t.prs.length ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {t.ticket ? <TicketLink id={t.ticket} /> : null}
          {t.prs.slice(0, 2).map((p) => (
            <PrLink key={p.url} pr={p} />
          ))}
        </div>
      ) : null}
    </motion.button>
  );
}

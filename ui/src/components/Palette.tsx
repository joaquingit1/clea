import { AnimatePresence, motion } from "motion/react";
import { Activity, Columns3, Home, MessageSquare, PenSquare, Search, Settings } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ChatSummary, Thread } from "../api";
import { t as tr, useLang } from "../i18n"; // tr: in this file t is a thread
import { ago, cx } from "../lib";
import { go, type Route } from "../router";
import { Glyph, Kbd } from "./ui";

interface Item {
  key: string;
  group: string;
  label: string;
  sub?: string;
  icon: ReactNode;
  to: Route;
}

export function Palette({ open, onClose, threads, chats }: { open: boolean; onClose: () => void; threads: Thread[]; chats: ChatSummary[] }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const lang = useLang(); // the list is memoized text: rebuild it when the language changes

  useEffect(() => {
    if (open) {
      setQ("");
      setSel(0);
      setTimeout(() => input.current?.focus(), 10);
    }
  }, [open]);

  const items = useMemo<Item[]>(() => {
    const s = q.trim().toLowerCase();
    const quick = tr("palette.quick"), inThreads = tr("palette.threads"), inChats = tr("palette.chats");
    const nav: Item[] = [
      { key: "new", group: quick, label: tr("nav.newChat"), icon: <PenSquare size={15} />, to: { page: "chat", conv: null } },
      { key: "board", group: quick, label: tr("nav.home"), icon: <Home size={15} />, to: { page: "board" } },
      { key: "threads", group: quick, label: tr("nav.threads"), icon: <Columns3 size={15} />, to: { page: "threads" } },
      { key: "activity", group: quick, label: tr("nav.activity"), icon: <Activity size={15} />, to: { page: "activity" } },
      { key: "settings", group: quick, label: tr("nav.settings"), icon: <Settings size={15} />, to: { page: "settings" } },
    ];
    const match = (txt: string) => !s || txt.toLowerCase().includes(s);
    const ts: Item[] = threads
      .filter((t) => match([t.headline, t.origin, t.ticket ?? "", ...t.prs.map((p) => p.label), `#${t.id}`].join(" ")))
      .sort((a, b) => b.updated - a.updated)
      .slice(0, s ? 30 : 8)
      .map((t) => ({
        key: `t${t.id}`,
        group: inThreads,
        label: t.headline,
        sub: [t.ticket, ago(t.updated)].filter(Boolean).join(" · "),
        icon: <Glyph phase={t.phase} size={7} />,
        to: { page: "thread", id: t.id },
      }));
    const cs: Item[] = chats
      .filter((c) => match(c.title))
      .slice(0, s ? 10 : 4)
      .map((c) => ({ key: c.conv, group: inChats, label: c.title || tr("sidebar.chat"), sub: ago(c.at), icon: <MessageSquare size={14} />, to: { page: "chat", conv: c.conv } }));
    return [...nav.filter((n) => match(n.label)), ...ts, ...cs];
  }, [q, threads, chats, lang]);

  useEffect(() => setSel(0), [q]);

  const pick = (i: Item) => {
    go(i.to);
    onClose();
  };

  let lastGroup = "";
  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          className="fixed inset-0 z-50 flex items-start justify-center bg-[rgba(0,0,0,0.12)] backdrop-blur-[2px] pt-[14vh]"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          onMouseDown={onClose}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -4 }}
            transition={{ type: "spring", duration: 0.3, bounce: 0.05 }}
            onMouseDown={(e) => e.stopPropagation()}
            className="glass-strong w-[560px] max-w-[92vw] overflow-hidden rounded-2xl p-1 shadow-[var(--shadow-lg)]"
          >
            <div className="flex items-center gap-2 px-2.5">
              <Search size={15} className="text-fg-3" />
              <input
                ref={input}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setSel((s) => Math.min(s + 1, items.length - 1));
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setSel((s) => Math.max(s - 1, 0));
                  } else if (e.key === "Enter" && items[sel]) {
                    pick(items[sel]);
                  } else if (e.key === "Escape") onClose();
                }}
                placeholder={tr("palette.placeholder")}
                className="h-10 w-full bg-transparent text-[14px] text-fg outline-none placeholder:text-fg-3"
              />
              <Kbd>esc</Kbd>
            </div>
            <div className="max-h-[52vh] overflow-y-auto">
              {items.map((it, i) => {
                const head = it.group !== lastGroup ? it.group : null;
                lastGroup = it.group;
                return (
                  <div key={it.key}>
                    {head ? <p className="px-2 pt-2 pb-1 text-[13px] text-fg-3">{head}</p> : null}
                    <button
                      onMouseMove={() => setSel(i)}
                      onClick={() => pick(it)}
                      className={cx(
                        "flex h-8 w-full items-center gap-2.5 rounded-lg px-2 text-left text-[14px] text-fg",
                        i === sel ? "bg-active" : "opacity-75",
                      )}
                    >
                      <span className="grid w-4 place-items-center text-fg-3">{it.icon}</span>
                      <span className="truncate">{it.label}</span>
                      {it.sub ? <span className="ml-auto shrink-0 pl-3 text-[13px] text-fg-3 tabular-nums">{it.sub}</span> : null}
                    </button>
                  </div>
                );
              })}
              {!items.length ? <p className="px-3 py-6 text-center text-[13px] text-fg-3">{tr("palette.none")}</p> : null}
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

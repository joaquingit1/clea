import { Activity, AlertTriangle, Check, Columns3, GitMerge, Home, Moon, PenSquare, Search, Settings, Sun } from "lucide-react";
import { motion } from "motion/react";
import { useState, type ReactNode } from "react";
import { PR_GROUPS, byOpened, type Board, type ChatSummary, type OwnPr, type Thread } from "../api";
import { rich, t as tr } from "../i18n"; // tr: in this file t is a thread
import { ago, cx } from "../lib";
import { desktop } from "../desktop";
import { href, type Route } from "../router";
import { Glyph, Kbd, Lockup, Spinner } from "./ui";

/** Codex-style trailing time: "now", "12 min", "3 h", "2 d". */
function short(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return tr("time.now");
  if (s < 3600) return tr("time.min", { n: Math.floor(s / 60) });
  if (s < 86400) return tr("time.h", { n: Math.floor(s / 3600) });
  return tr("time.d", { n: Math.floor(s / 86400) });
}

export function Sidebar({
  route,
  board,
  threads,
  chats,
  onPalette,
}: {
  route: Route;
  board: Board | null;
  threads: Thread[];
  chats: ChatSummary[];
  onPalette: () => void;
}) {
  const needs = threads.filter((t) => t.phase === "you").sort((a, b) => b.created - a.created);
  // newest first: PRs and asks by when they were opened, activity by when it happened
  const mergeable = (board?.prs ?? [])
    .filter((p) => p.state === "merge")
    .sort((a, b) => (b.created ?? "").localeCompare(a.created ?? ""));
  const prGroups = PR_GROUPS.map((g) => ({ ...g, prs: (board?.prs ?? []).filter((p) => g.states.includes(p.state)).sort(byOpened) })).filter((g) => g.prs.length);
  const prsForYou = prGroups.find((g) => g.k === "you")?.prs.length ?? 0;
  const reviews = needs.filter((t) => t.kind === "Review");
  const others = needs.filter((t) => t.kind !== "Review");
  const running = threads.filter((t) => t.phase === "running").sort((a, b) => b.updated - a.updated);
  // everything else Clea is on or just did, like the agent apps' thread list: in flight, then the last 48 h by day
  const waiting = threads.filter((t) => t.phase === "waiting").sort((a, b) => b.updated - a.updated);
  const since = Date.now() / 1000 - 2 * 86400;
  const recent = threads.filter((t) => (t.phase === "done" || t.phase === "closed") && t.updated > since).sort((a, b) => b.updated - a.updated);
  const today = new Date().toDateString();
  const recentToday = recent.filter((t) => new Date(t.updated * 1000).toDateString() === today);
  const recentBefore = recent.filter((t) => new Date(t.updated * 1000).toDateString() !== today);
  const freeChats = chats.filter((c) => !c.item);
  const active = (r: Route["page"]) => route.page === r;
  const working = running.length > 0;
  const h = board?.health;

  return (
    <aside className="app-sidebar flex h-full w-[275px] shrink-0 flex-col bg-sidebar">
      <div className="sidebar-head flex h-12 items-center gap-2.5 px-4 pt-1">
        {/* the logo where the status text was; the mark itself says the state: it speaks while Clea works */}
        <span className="ml-auto flex items-center" title={tr(h?.paused_until ? "sidebar.paused" : working ? "sidebar.working" : "sidebar.idle")}>
          <Lockup height={17} busy={working} state="intro" className={h?.paused_until ? "text-amber" : "text-brand-ink"} />
        </span>
      </div>

      <div className="space-y-px px-2 pt-1">
        <a
          href={href({ page: "chat", conv: null })}
          className="flex h-[30px] items-center gap-2 rounded-[10px] px-2 text-[14px] text-fg transition hover:bg-hover"
        >
          <PenSquare size={16} strokeWidth={1.8} className="text-fg-2" />
          {tr("nav.newChat")}
        </a>
        <button
          onClick={onPalette}
          className="flex h-[30px] w-full items-center gap-2 rounded-[10px] px-2 text-[14px] text-fg transition hover:bg-hover"
        >
          <Search size={16} strokeWidth={1.8} className="text-fg-2" />
          {tr("nav.search")}
          <span className="ml-auto flex gap-0.5">
            <Kbd>⌘</Kbd>
            <Kbd>K</Kbd>
          </span>
        </button>
        <Nav to={href({ page: "board" })} on={active("board")} icon={<Home size={16} strokeWidth={1.8} />}>
          {tr("nav.home")}
          {needs.length + mergeable.length + prsForYou ? <Count tone="amber">{needs.length + mergeable.length + prsForYou}</Count> : null}
        </Nav>
        <Nav to={href({ page: "threads" })} on={active("threads")} icon={<Columns3 size={16} strokeWidth={1.8} />}>
          {tr("nav.threads")}
          {running.length ? <Count tone="accent">{running.length}</Count> : null}
        </Nav>
        <Nav to={href({ page: "activity" })} on={active("activity")} icon={<Activity size={16} strokeWidth={1.8} />}>
          {tr("nav.activity")}
        </Nav>
      </div>

      <div className="mt-5 min-h-0 flex-1 space-y-5 overflow-y-auto px-2 pb-4">
        {running.length ? (
          <Group title={tr("sidebar.workingNow")}>
            {running.map((t) => (
              <ThreadRow key={t.id} t={t} on={route.page === "thread" && route.id === t.id} />
            ))}
          </Group>
        ) : null}
        {mergeable.length ? (
          <Group title={tr("sidebar.toMerge", { n: mergeable.length })}>
            {mergeable.map((p) => (
              <PrRow key={p.url} p={p} on={route.page === "pr" && route.repo === p.repo && route.n === p.number} />
            ))}
          </Group>
        ) : null}
        {prGroups.map((g) => (
          <Group key={g.k} title={`${tr(`prGroup.${g.k}.label`)} · ${g.prs.length}`} hint={tr(`prGroup.${g.k}.hint`)}>
            {g.prs.map((p) => (
              <PrRow key={p.url} p={p} on={route.page === "pr" && route.repo === p.repo && route.n === p.number} />
            ))}
          </Group>
        ))}
        {reviews.length ? (
          <Group title={tr("sidebar.reviews", { n: reviews.length })}>
            {reviews.slice(0, 8).map((t) => (
              <ThreadRow key={t.id} t={t} on={route.page === "thread" && route.id === t.id} />
            ))}
          </Group>
        ) : null}
        <Group title={tr("sidebar.messages")} more={others.length > 8 ? { to: href({ page: "threads" }), n: others.length } : undefined}>
          {others.slice(0, 8).map((t) => (
            <ThreadRow key={t.id} t={t} on={route.page === "thread" && route.id === t.id} />
          ))}
          {!others.length ? <p className="px-2 py-1 text-[13px] text-fg-3">{tr("sidebar.nothing")}</p> : null}
        </Group>
        {waiting.length ? (
          <Group title={tr("sidebar.inProgress", { n: waiting.length })} hint={tr("sidebar.inProgressHint")}>
            {waiting.slice(0, 12).map((t) => (
              <ThreadRow key={t.id} t={t} on={route.page === "thread" && route.id === t.id} />
            ))}
          </Group>
        ) : null}
        {recentToday.length ? (
          <Group title={tr("sidebar.today", { n: recentToday.length })} more={recentToday.length > 15 ? { to: href({ page: "activity" }), n: recentToday.length } : undefined}>
            {recentToday.slice(0, 15).map((t) => (
              <ThreadRow key={t.id} t={t} on={route.page === "thread" && route.id === t.id} />
            ))}
          </Group>
        ) : null}
        {recentBefore.length ? (
          <Group title={tr("sidebar.yesterday", { n: recentBefore.length })} more={recentBefore.length > 10 ? { to: href({ page: "activity" }), n: recentBefore.length } : undefined}>
            {recentBefore.slice(0, 10).map((t) => (
              <ThreadRow key={t.id} t={t} on={route.page === "thread" && route.id === t.id} />
            ))}
          </Group>
        ) : null}
        {freeChats.length ? (
          <Group title={tr("sidebar.chats")}>
            {freeChats.slice(0, 12).map((c) => (
              <a
                key={c.conv}
                href={href({ page: "chat", conv: c.conv })}
                className={cx(
                  "relative isolate flex h-[30px] items-center gap-2 rounded-[10px] px-2 text-[14px] text-fg transition",
                  !(route.page === "chat" && route.conv === c.conv) && "hover:bg-hover",
                )}
              >
                {route.page === "chat" && route.conv === c.conv ? <Sel /> : null}
                <span className="grid w-4 place-items-center">{c.thinking ? <Spinner size={12} /> : null}</span>
                <span className="fade-truncate min-w-0 flex-1">{c.title || tr("sidebar.chat")}</span>
                <span className="shrink-0 text-[13px] text-fg-3 tabular-nums">{short(c.at)}</span>
              </a>
            ))}
          </Group>
        ) : null}
      </div>

      <Footer board={board} settings={route.page === "settings"} />
    </aside>
  );
}

function Footer({ board, settings }: { board: Board | null; settings: boolean }) {
  const h = board?.health;
  const [theme, setTheme] = useState<string>(() => document.documentElement.dataset.theme || "");
  const dark = theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  const toggle = () => {
    const t = dark ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    desktop?.setTheme?.(t);
    try {
      localStorage.setItem("theme", t);
    } catch {
      /* fine: this tab only */
    }
    setTheme(t);
  };
  return (
    <div className="px-3 py-2.5">
      {h?.wrong_account ? (
        <div className="mb-2 flex gap-2 rounded-xl bg-amber-soft px-2.5 py-2 text-[12px] leading-snug text-amber">
          <AlertTriangle size={14} className="mt-px shrink-0" />
          <span>{rich("sidebar.wrongAccount", { cmd: <code className="font-mono">claude auth login</code> })}</span>
        </div>
      ) : null}
      <div className="flex items-center gap-2 text-[12px] text-fg-3">
        <span className="font-mono">{h?.rev ? h.rev.slice(0, 7) : "…"}</span>
        <span>·</span>
        <span>{board ? tr("sidebar.updated", { ago: ago(board.at) }) : tr("sidebar.connecting")}</span>
        <a
          href={href({ page: "settings" })}
          aria-label={tr("nav.settings")}
          title={tr("nav.settings")}
          aria-current={settings ? "page" : undefined}
          className={cx("ml-auto grid size-7 place-items-center rounded-md hover:bg-hover hover:text-fg", settings && "bg-active text-fg")}
        >
          <Settings size={14} />
        </a>
        <button onClick={toggle} aria-label={tr("sidebar.theme")} title={tr("sidebar.theme")} className="grid size-7 place-items-center rounded-md hover:bg-hover hover:text-fg">
          {dark ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
    </div>
  );
}

function Nav({ to, on, icon, children }: { to: string; on: boolean; icon: ReactNode; children: ReactNode }) {
  return (
    <a
      href={to}
      aria-current={on ? "page" : undefined}
      className={cx("relative isolate flex h-[30px] items-center gap-2 rounded-[10px] px-2 text-[14px] text-fg transition", !on && "hover:bg-hover")}
    >
      {on ? <Sel /> : null}
      <span className="text-fg-2">{icon}</span>
      {children}
    </a>
  );
}

function Count({ children, tone }: { children: ReactNode; tone: "amber" | "accent" }) {
  return (
    <span
      className={cx(
        "ml-auto rounded-full px-1.5 text-[12px] leading-[18px] tabular-nums",
        tone === "amber" ? "bg-amber-soft text-amber" : "bg-accent-soft text-accent",
      )}
    >
      {children}
    </span>
  );
}

function Group({ title, children, more, hint }: { title: string; children: ReactNode; more?: { to: string; n: number }; hint?: string }) {
  return (
    <div>
      <div className="mb-1 flex items-center px-2 text-[13px] text-fg-3" title={hint}>
        {title}
        {more ? (
          <a href={more.to} className="ml-auto hover:text-fg-2">
            {tr("sidebar.more", { n: more.n })}
          </a>
        ) : null}
      </div>
      <div className="space-y-px">{children}</div>
    </div>
  );
}

function PrRow({ p, on }: { p: OwnPr; on: boolean }) {
  return (
    <a
      href={href({ page: "pr", repo: p.repo, n: p.number })}
      aria-current={on ? "page" : undefined}
      title={`${p.label}${p.ticket ? ` · ${p.ticket}` : ""} · ${p.why}`}
      className={cx("relative isolate flex h-[30px] items-center gap-2 rounded-[10px] px-2 text-[14px] text-fg transition", !on && "hover:bg-hover")}
    >
      {on ? <Sel /> : null}
      <span className="grid w-4 shrink-0 place-items-center">
        <GitMerge
          size={13}
          strokeWidth={2}
          className={p.state !== "merge" ? (["blocked", "decide", "draft"].includes(p.state) ? "text-amber" : "text-fg-3") : p.approval === "human" ? "text-[#1f9d55]" : "text-[#e0700f]"}
        />
      </span>
      <span className="fade-truncate min-w-0 flex-1 first-letter:uppercase">{p.ticket ? `${p.ticket} · ${p.title}` : p.title}</span>
    </a>
  );
}

function ThreadRow({ t, on }: { t: Thread; on: boolean }) {
  return (
    <a
      href={href({ page: "thread", id: t.id })}
      title={`${t.headline}\n${t.last || t.stage}`}
      aria-current={on ? "page" : undefined}
      className={cx("relative isolate flex h-[30px] items-center gap-2 rounded-[10px] px-2 text-[14px] text-fg transition", !on && "hover:bg-hover")}
    >
      {on ? <Sel /> : null}
      <span className="grid w-4 shrink-0 place-items-center">
        {t.phase === "done" ? <Check size={12} strokeWidth={2.4} className="text-green" /> : <Glyph phase={t.phase} size={6} />}
      </span>
      <span className={cx("fade-truncate min-w-0 flex-1", t.phase === "closed" && "text-fg-2")}>{t.headline}</span>
      <span className="shrink-0 text-[13px] text-fg-3 tabular-nums">{short(t.updated)}</span>
    </a>
  );
}

/** The selection: one highlight that slides from the row you left to the row you opened. */
function Sel() {
  return (
    <motion.span
      layoutId="sidebar-sel"
      transition={{ type: "spring", stiffness: 520, damping: 42, mass: 0.8 }}
      className="absolute inset-0 -z-10 rounded-[10px] bg-active"
    />
  );
}

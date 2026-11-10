import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUpRight, GitPullRequest, Ticket } from "lucide-react";
import type { ReactNode } from "react";
import type { Phase, PrRef } from "../api";
import { t } from "../i18n";
import { cx, jira, slackToMd } from "../lib";
import { Mark } from "./brand";

export function Glyph({ phase, size = 8 }: { phase: Phase; size?: number }) {
  const color = {
    you: "text-amber",
    running: "text-accent",
    waiting: "text-fg-3",
    done: "text-green",
    closed: "text-fg-3",
  }[phase];
  if (phase === "running") return <Mark size={size + 6} busy className="text-brand-ink" />; // Clea at work: the mark speaking
  if (phase === "waiting" || phase === "closed")
    return (
      <span
        className={cx("inline-block shrink-0 rounded-full border-[1.5px] border-current", color)}
        style={{ width: size, height: size }}
      />
    );
  return (
    <span
      className={cx("inline-block shrink-0 rounded-full bg-current", color)}
      style={{ width: size, height: size }}
    />
  );
}

export function Tag({ children, tone = "neutral", mono }: { children: ReactNode; tone?: Tone; mono?: boolean }) {
  return (
    <span
      className={cx(
        "inline-flex h-[20px] items-center gap-1 rounded-md px-1.5 text-[11.5px] font-medium leading-none whitespace-nowrap",
        mono && "font-mono text-[11px] font-normal",
        TONE[tone],
      )}
    >
      {children}
    </span>
  );
}

type Tone = "neutral" | "amber" | "accent" | "green" | "red" | "blue";
const TONE: Record<Tone, string> = {
  neutral: "bg-hover text-fg-2",
  amber: "bg-amber-soft text-amber",
  accent: "bg-accent-soft text-accent",
  green: "bg-green-soft text-green",
  red: "bg-red-soft text-red",
  blue: "bg-blue-soft text-blue",
};

export function CiDot({ ci }: { ci: PrRef["ci"] }) {
  if (!ci) return null;
  const c = { green: "bg-green", red: "bg-red", running: "bg-amber animate-pulse" }[ci];
  return <span title={t(`ci.${ci}`)} className={cx("inline-block size-1.5 rounded-full", c)} />;
}

export function PrLink({ pr, to }: { pr: { url: string; label: string; ci?: PrRef["ci"] }; to?: string }) {
  return (
    <a
      href={to ?? pr.url}
      target={to ? undefined : "_blank"}
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="group inline-flex h-[22px] items-center gap-1.5 rounded-full bg-blue-soft py-0.5 ps-2 pe-2.5 text-[12px] text-blue transition hover:brightness-105"
    >
      <GitPullRequest size={12} strokeWidth={2} />
      {pr.label}
      {pr.ci ? <CiDot ci={pr.ci} /> : null}
    </a>
  );
}

export function TicketLink({ id }: { id: string }) {
  return (
    <a
      href={jira(id)}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="inline-flex h-[22px] items-center gap-1 rounded-full bg-purple-soft py-0.5 ps-2 pe-2.5 text-[12px] text-purple transition hover:brightness-105"
    >
      <Ticket size={12} strokeWidth={2} />
      {id}
    </a>
  );
}

export function SectionHead({ title, count, right }: { title: string; count?: number; right?: ReactNode }) {
  return (
    <div className="mb-2.5 flex items-center gap-2 px-1">
      <h2 className="text-[13px] font-medium text-fg-2">{title}</h2>
      {count !== undefined ? <span className="text-[13px] text-fg-3 tabular-nums">{count}</span> : null}
      <div className="ml-auto">{right}</div>
    </div>
  );
}

export function Md({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cx("prose-agent", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {slackToMd(children)}
      </ReactMarkdown>
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-4 min-w-4 items-center justify-center rounded-md bg-current/10 px-1.5 font-sans text-[11px] text-fg-3">
      {children}
    </kbd>
  );
}

export function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="inline-flex items-center gap-0.5 text-fg-2 underline decoration-line-strong underline-offset-[3px] transition hover:text-fg hover:decoration-accent"
    >
      {children}
      <ArrowUpRight size={12} className="opacity-60" />
    </a>
  );
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx("glass overflow-hidden rounded-2xl", className)}>
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-4 py-6 text-center text-[13px] text-fg-3">{children}</div>;
}

/** Codex's running glyph: a small ring spinner at 70% ink (or the color given, inside a filled button). */
export function Spinner({ size = 12, className = "text-fg/70" }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" className={cx("shrink-0 animate-spin", className)} aria-label={t("common.working")}>
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeOpacity="0.2" strokeWidth="2" />
      <path d="M14 8a6 6 0 0 0-6-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export { AppIcon, Lockup, Mark } from "./brand";

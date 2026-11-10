import { useEffect, useRef, useState } from "react";

export type Phase = "you" | "running" | "waiting" | "done" | "closed";

export interface PrRef {
  url: string;
  label: string;
  ci: "green" | "red" | "running" | null;
  approved: boolean;
  linked: boolean;
}

export interface Thread {
  id: number;
  kind: string;
  status: string;
  stage: string;
  phase: Phase;
  headline: string;
  origin: string;
  ticket: string | null;
  need: string;
  last: string;
  updated: number;
  created: number;
  urgency: number;
  prs: PrRef[];
  asks: number;
  next: NextStep | null;
}

/** What happens next on a case and who has to move. "you" always comes with something to press. */
export interface NextStep {
  who: "you" | "others" | "clea" | "none";
  kind: "ask" | "blocked" | "need" | "merge" | "pr" | "decide" | "waiting" | "running" | "approve_review";
  title: string;
  detail: string;
  actions: CaseAction[];
  prs: OwnPr[];
}

export type CaseAction = "go" | "go_with" | "retry" | "retry_with" | "reply" | "drop" | "approve_review";

export interface Ask {
  id: number;
  kind: string;
  what: string;
  text: string;
  comments: { path: string; line: number; body: string }[];
  slack: string | null;
  at: number;
  expired: boolean;
}

export interface Event {
  at: number;
  stage: string;
  title: string;
  note: string;
}

export interface ThreadDetail extends Omit<Thread, "asks"> {
  bullets: string[];
  analysis: string;
  cause: "probado" | "probable" | "sin confirmar" | "no aplica" | "";
  technical: { problem: string; cause: string; recommendation: string } | null;
  source: { text: string; channel: string | null; link: string | null; author: string | null };
  report: string;
  detail: string;
  asks: Ask[];
  repro: { verdict: string | null; files: string[]; failure: string | null; green: boolean } | null;
  caveats: string[];
  blocked: string | null;
  events: Event[];
  cost: number;
  updates: { text: string }[];
}

export interface OwnPr {
  repo: string;
  number: number;
  head: string | null;
  approval: "human" | "coderabbit" | null;
  ticket: string | null;
  repro: {
    e2e: boolean;
    verdict: string | null;
    kind: string | null;
    files: string[];
    failure: string;
    why: string;
    red_at: number | null;
    green_at: number | null;
    gate_green_at?: number | null;
    green_head: string | null;
    backfill?: boolean;
    branch?: string | null;
    green_fail?: string | null;
    green_on?: string | null;
    rejected?: string | null;
    question?: string | null;
  } | null;
  label: string;
  url: string;
  title: string;
  state: "merge" | "decide" | "queued" | "review" | "draft" | "blocked" | "working";
  why: string;
  after: string[];
  created: string | null;
  case: number | null;
  ci: "green" | "red" | "running" | null;
  threads: number;
  updated: number | null;
}

/** His non-ready PRs, by what they need: him first, then the merge queue, then what waits on others. Every PR shows,
 * none are cut. Each group's name and
 * hint are read at render time, t(`prGroup.${k}.label`) and.hint, so they follow the language. */
export const PR_GROUPS: { k: "you" | "queued" | "review" | "working"; states: OwnPr["state"][] }[] = [
  { k: "you", states: ["decide", "blocked", "draft"] },
  { k: "queued", states: ["queued"] },
  { k: "review", states: ["review"] },
  { k: "working", states: ["working"] },
];

export const byOpened = (a: OwnPr, b: OwnPr) => (b.created ?? "").localeCompare(a.created ?? "");

export interface Board {
  at: number;
  greeting: string;
  needs: Thread[];
  running: Thread[];
  waiting: Thread[];
  shipped: Thread[];
  dropped: Thread[];
  prs: OwnPr[];
  blocking: { url: string | null; label: string; text: string }[];
  stats: { prs_week: number; done_week: number; cost_today: number; open: number };
  health: { rev: string | null; paused_until: number | null; wrong_account: string | null; worker_beat: number | null };
}

export interface Activity {
  id: number;
  at: number;
  stage: string;
  title: string;
  headline: string;
  note: string;
  kind: string;
}

export interface ChatSummary {
  conv: string;
  title: string;
  at: number;
  item: number | null;
  n: number;
  thinking: boolean;
}

export interface ChatMessage {
  id: number;
  role: "user" | "agent" | "system";
  text: string;
  at: number;
  meta: { actions?: string[]; secs?: number; pending?: boolean; ack?: boolean; fast?: boolean; steps?: number; streaming?: boolean };
}

export interface Conversation {
  conv: string;
  messages: ChatMessage[];
  thinking: boolean;
  since: number | null;
  step?: string | null;
}

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

export async function sendChat(text: string, conv?: string | null, item?: number | null): Promise<string> {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-AP-Console": "1" },
    body: JSON.stringify({ text, conv: conv || undefined, item: item ?? undefined }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j.conv;
}

/** True while the window is in front of him. In the background Clea idles: polls stretch out and animations pause
 * (index.css, html.away), since a backgrounded renderer still repainting every spinner cost a third of a core. */
function awake(): boolean {
  return !document.hidden && document.hasFocus();
}
const AWAY_MS = 30_000;
const wakers = new Set<() => void>();
function syncAway() {
  const up = awake();
  document.documentElement.classList.toggle("away", !up);
  if (up) wakers.forEach((w) => w());
}
window.addEventListener("focus", syncAway);
window.addEventListener("blur", syncAway);
document.addEventListener("visibilitychange", syncAway);
syncAway();

/** Poll a JSON endpoint; keeps the last good value through a failed refresh. An unchanged response keeps the same
 * object, so nothing re-renders; in the background it polls every 30 s at most and catches up on return. */
export function usePoll<T>(url: string | null, ms: number): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const tick = useRef(0);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!url) {
      setData(null);
      return;
    }
    let alive = true;
    let last = "";
    let h: ReturnType<typeof setTimeout> | undefined;
    let busy = false;
    const my = ++tick.current;
    const run = async () => {
      if (busy) return; // one request and one timer at a time, even when a wake lands mid-fetch
      busy = true;
      clearTimeout(h);
      try {
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) throw new Error(`${r.status} ${url}`);
        const text = await r.text();
        if (alive && my === tick.current) {
          if (text !== last) {
            last = text;
            setData(JSON.parse(text) as T);
          }
          setError(null);
        }
      } catch (e) {
        if (alive) setError(String(e));
      }
      busy = false;
      if (alive) h = setTimeout(run, awake() ? ms : Math.max(ms, AWAY_MS));
    };
    run();
    // back in front: fresh data now instead of at the end of a long background wait
    const wake = () => run();
    wakers.add(wake);
    return () => {
      alive = false;
      clearTimeout(h);
      wakers.delete(wake);
    };
  }, [url, ms, nonce]);
  return { data, error, refresh: () => setNonce((n) => n + 1) };
}

export interface AskState {
  state: "open" | "waiting" | "working" | "done" | "declined" | "error";
  decision?: string;
  outcome?: string | null;
  error?: string;
}

/** approve (the guard asks for Touch ID), reject, or revise with what to change. */
export async function decideAsk(id: number, decision: "approve" | "reject" | "revise", text = ""): Promise<AskState> {
  const r = await fetch(`/api/ask/${id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-AP-Console": "1" },
    body: JSON.stringify({ decision, text }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

export interface MergeState {
  state: "open" | "waiting" | "done" | "declined" | "error";
  error?: string;
}

/** Squash-merge one of his ready PRs; the guard asks for Touch ID. */
export async function mergePr(repo: string, n: number): Promise<MergeState> {
  const r = await fetch(`/api/pr/${repo}/${n}/merge`, { method: "POST", headers: { "Content-Type": "application/json", "X-AP-Console": "1" }, body: "{}" });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

export interface CaseActionState {
  state: "open" | "waiting" | "done" | "declined" | "error";
  message?: string;
  error?: string;
}

/** go / retry / drop act at once; approve_review waits on Touch ID in the guard and is polled. */
export async function caseAction(id: number, action: "go" | "retry" | "drop" | "approve_review", text = ""): Promise<CaseActionState> {
  const r = await fetch(`/api/case/${id}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-AP-Console": "1" },
    body: JSON.stringify({ text }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

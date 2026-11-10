import { getLang, locale, t } from "./i18n";

export function cx(...xs: (string | false | null | undefined)[]): string {
  return xs.filter(Boolean).join(" ");
}

// one formatter per language, built the first time that language is shown
const rtfs: Record<string, Intl.RelativeTimeFormat> = {};
function rtf(): Intl.RelativeTimeFormat {
  const l = getLang();
  return (rtfs[l] ??= new Intl.RelativeTimeFormat(l, { numeric: "auto", style: "short" }));
}

/** "hace 3 min", "ayer", "hace 4 días" (or "3 min. ago", "yesterday") from a unix timestamp in seconds. */
export function ago(ts: number | null | undefined, nowMs = Date.now()): string {
  if (!ts) return "";
  const s = Math.round(ts - nowMs / 1000);
  const a = Math.abs(s);
  if (a < 45) return t("time.now");
  if (a < 3600) return rtf().format(Math.round(s / 60), "minute");
  if (a < 86400) return rtf().format(Math.round(s / 3600), "hour");
  if (a < 86400 * 30) return rtf().format(Math.round(s / 86400), "day");
  return rtf().format(Math.round(s / (86400 * 30)), "month");
}

/** 24 hours in both languages: the time sits in narrow fixed columns. */
export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

export function dayLabel(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date();
  const y = new Date();
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return t("day.today");
  if (d.toDateString() === y.toDateString()) return t("day.yesterday");
  const s = d.toLocaleDateString(locale(), { weekday: "long", day: "numeric", month: "short" });
  return s[0].toUpperCase() + s.slice(1);
}

export function since(ts: number | null, nowMs = Date.now()): string {
  if (!ts) return "";
  const s = Math.max(0, Math.round(nowMs / 1000 - ts));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/** Slack-style <url|label> links become Markdown links so one renderer handles both. A user group reads as its handle
 *  when setup knows it (site.groups), never as a name written into the code. */
export function slackToMd(text: string): string {
  return text
    .replace(/<!subteam\^([A-Z0-9]+)(?:\|([^>]+))?>/g, (_m, id: string, name?: string) => name ?? site.groups[id] ?? t("slack.group"))
    .replace(/<!(here|channel|everyone)>/g, "@$1")
    .replace(/<@([A-Z0-9]+)(?:\|([^>]+))?>/g, (_m, _id: string, name?: string) => (name ? `@${name}` : t("slack.someone")))
    .replace(/<(https?:\/\/[^|>]+)\|([^>]+)>/g, "[$2]($1)")
    .replace(/<(https?:\/\/[^>]+)>/g, (_m, u: string) => `[${label(u)}](${u})`)
    .replace(/(^|\s)\*([^*\n]+)\*(?=\s|[.,:;]|$)/g, "$1**$2**");
}

/** The workspace this install works in (config.json via /api/config, user groups via setup): Clea's own code names no
 *  organization. */
export const site: { githubOrg: string; jiraBase: string; groups: Record<string, string> } = { githubOrg: "", jiraBase: "", groups: {} };

export async function loadSite(): Promise<void> {
  try {
    const r = await fetch("/api/config");
    if (r.ok) {
      const c = await r.json();
      site.githubOrg = c.github_org || "";
      site.jiraBase = (c.jira_base || "").replace(/\/$/, "");
    }
  } catch {
    /* worker down: links fall back to plain text until the next launch */
  }
}

export const githubPr = (repo: string, n: number) => `https://github.com/${site.githubOrg}/${repo}/pull/${n}`;

export function label(url: string): string {
  let m = url.match(/github\.com\/[\w.-]+\/([\w.-]+)\/pull\/(\d+)/);
  if (m) return `${m[1]}#${m[2]}`;
  m = url.match(/atlassian\.net\/browse\/([A-Z]+-\d+)/);
  if (m) return m[1];
  if (url.includes("slack.com/archives/")) return t("link.thread");
  m = url.match(/^https?:\/\/(?:www\.)?([^/]+)/);
  return m ? m[1] : t("link.link");
}

export const jira = (key: string) => (site.jiraBase ? `${site.jiraBase}/browse/${key}` : undefined);

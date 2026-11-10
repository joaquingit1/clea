import { useSyncExternalStore } from "react";
import { setLang, t, type Lang } from "./i18n";
import { site } from "./lib";

/** The setup API: what the worker knows about this install and the calls that change it. Tokens and keys go in, only
 *  their status comes out. */
export type StepId = "welcome" | "model" | "slack" | "code" | "team" | "tickets" | "autonomy";
export type Autonomy = "watch" | "ask" | "auto";
export type ChannelRole = "team" | "prs" | "alerts" | "watch";
export type ProviderId = "claude-subscription" | "claude-api" | "openai-chatgpt" | "codex-login" | "openai-api";

export interface SetupStep {
  id: StepId;
  done: boolean;
  optional: boolean;
  summary: string;
}

export interface ProviderState {
  id: ProviderId;
  vendor: "Anthropic" | "OpenAI";
  engine: "claude" | "codex";
  auth: "subscription" | "api_key" | "siwc";
  cli: { name: "claude" | "codex"; installed: boolean; version: string; install_url: string };
  ready: boolean;
  selected: boolean;
  account: string;
  plan: string;
  needs: null | "install" | "login" | "key";
  detail: string;
  expires_at: number | null;
}

export interface SlackState {
  connected: boolean;
  workspace: { name: string; url: string; team_id: string } | null;
  user: { id: string; name: string } | null;
  missing_scopes: string[];
  socket: boolean;
  channels: ChannelChoice[];
  usergroups: { id: string; handle: string }[];
  keywords: string[];
  scan_dms: boolean;
  mentions: boolean;
  manifest_url: string;
}
export interface ChannelChoice {
  id: string;
  name: string;
  role: ChannelRole;
}

export interface RepoChoice {
  name: string;
  full_name: string;
  path: string;
  default: string;
}
export interface GithubState {
  cli: { installed: boolean; version: string };
  connected: boolean;
  login: string;
  org: string;
  repos: RepoChoice[];
}

export interface TeamMember {
  slack_id: string;
  name: string;
  github: string;
}
export interface TeamState {
  members: TeamMember[];
  area: string;
  keywords: string[];
}

export interface JiraState {
  enabled: boolean;
  connected: boolean;
  base: string;
  email: string;
  account: string;
  projects: string[];
  assigned: boolean;
  mentions: boolean;
  jql: string;
}

export interface SetupState {
  edition: "internal" | "release";
  complete: boolean;
  running: boolean;
  language: Lang;
  user: { name: string };
  steps: SetupStep[];
  provider: { engine: "" | "claude" | "codex"; auth: "" | "subscription" | "api_key" | "siwc" };
  providers: ProviderState[];
  slack: SlackState;
  github: GithubState;
  team: TeamState;
  jira: JiraState;
  autonomy: Autonomy;
  /** False when the install has no telemetry source; absent on an older worker. */
  sees_production?: boolean;
}

export interface LoginState {
  id: string | null;
  state: "idle" | "waiting" | "done" | "error";
  url: string | null;
  error: string | null;
}
export interface SlackChannel {
  id: string;
  name: string;
  private: boolean;
  member: boolean;
  members: number;
  purpose: string;
}
export interface SlackGroup {
  id: string;
  handle: string;
  name: string;
  members: number;
  mine: boolean;
}
export interface SlackUser {
  id: string;
  name: string;
  real_name: string;
  title: string;
}
export interface GithubRepo {
  name: string;
  full_name: string;
  default: string;
  private: boolean;
  pushed_at: string;
  path: string;
  language: string;
}
export interface JiraProject {
  key: string;
  name: string;
}
export interface JiraPreview {
  count: number;
  sample: { key: string; summary: string; status: string }[];
}

/** A request the worker refused or never answered. `message` is what a person reads next to the control: the worker's
 *  own `error` text when it gave one. `missing` comes with a refused finish. */
export class SetupError extends Error {
  status: number;
  missing: StepId[];
  constructor(message: string, status: number, missing: StepId[] = []) {
    super(message);
    this.status = status;
    this.missing = missing;
  }
}

/** The text to show for anything a setup call threw. */
export function errorText(e: unknown): string {
  return e instanceof SetupError ? e.message : t("common.noAnswer");
}

async function call<T>(path: string, init?: RequestInit, ms = 30_000): Promise<T> {
  const ctl = new AbortController();
  const h = setTimeout(() => ctl.abort(), ms);
  let r: Response;
  try {
    r = await fetch(`/api/setup${path}`, { cache: "no-store", ...init, signal: ctl.signal });
  } catch {
    throw new SetupError(t("common.noAnswer"), 0);
  } finally {
    clearTimeout(h);
  }
  let body: unknown = null;
  try {
    body = await r.json();
  } catch {
    /* not JSON: a proxy's error page, handled as a failure below */
  }
  if (!r.ok) {
    const b = (body ?? {}) as { error?: string; missing?: StepId[] };
    throw new SetupError(b.error || t("common.noAnswer"), r.status, b.missing ?? []);
  }
  return body as T;
}

const get = <T,>(path: string) => call<T>(path);
const post = <T,>(path: string, body: unknown = {}) =>
  call<T>(path, { method: "POST", headers: { "Content-Type": "application/json", "X-AP-Console": "1" }, body: JSON.stringify(body) });
const q = encodeURIComponent;

export const api = {
  state: () => call<SetupState>("", undefined, 8000),
  profile: (name: string, language: Lang) => post<{ ok: true }>("/profile", { name, language }),
  provider: (id: ProviderId, api_key?: string) => post<{ ok: true; provider: ProviderState }>("/provider", api_key ? { id, api_key } : { id }),
  login: (id: ProviderId) => post<LoginState>("/provider/login", { id }),
  loginState: () => get<LoginState>("/provider/login"),
  loginCancel: () => post<{ ok: true }>("/provider/login/cancel"),
  logout: (id: ProviderId) => post<{ ok: true }>("/provider/logout", { id }),
  slackConnect: (user_token: string, app_token?: string) =>
    post<{ ok: true; slack: SlackState }>("/slack/connect", app_token ? { user_token, app_token } : { user_token }),
  slackChannels: (text: string) => get<SlackChannel[]>(`/slack/channels?q=${q(text)}`),
  slackGroups: () => get<SlackGroup[]>("/slack/usergroups"),
  slackUsers: (text: string) => get<SlackUser[]>(`/slack/users?q=${q(text)}`),
  slack: (body: Pick<SlackState, "channels" | "usergroups" | "keywords" | "scan_dms" | "mentions">) => post<{ ok: true; slack: SlackState }>("/slack", body),
  githubOrgs: () => get<{ login: string; orgs: { login: string; name: string }[] }>("/github/orgs"),
  githubRepos: (org: string, text: string) => get<GithubRepo[]>(`/github/repos?org=${q(org)}&q=${q(text)}`),
  github: (org: string, repos: RepoChoice[]) => post<{ ok: true; github: GithubState }>("/github", { org, repos }),
  team: (body: TeamState) => post<{ ok: true; team: TeamState }>("/team", body),
  teamSuggest: () => get<{ members: TeamMember[] }>("/team/suggest"),
  jiraConnect: (base: string, email: string, token: string) => post<{ ok: true; jira: JiraState; projects: JiraProject[] }>("/jira/connect", { base, email, token }),
  jira: (body: Pick<JiraState, "enabled" | "projects" | "assigned" | "mentions" | "jql">) => post<{ ok: true; jira: JiraState }>("/jira", body),
  jiraPreview: (body: Pick<JiraState, "projects" | "assigned" | "mentions" | "jql">) => post<JiraPreview>("/jira/preview", body),
  autonomy: (level: Autonomy) => post<{ ok: true }>("/autonomy", { level }),
  finish: () => post<{ ok: true; missing: StepId[] }>("/finish"),
  reopen: () => post<{ ok: true }>("/reopen"),
};

/* ---- the one copy of SetupState the app reads -------------------------------------------------------------------- */

/** Where the app stands: still asking, the worker cannot be reached, or the state itself. `legacy` is a worker from
 *  before the setup API (it answers 404): that install was configured by hand, so the board loads as it always did. */
export type Boot = { phase: "loading" } | { phase: "down" } | { phase: "ready"; state: SetupState | null; legacy: boolean };

let boot: Boot = { phase: "loading" };
const subs = new Set<() => void>();
let serverLang: Lang | null = null;

function publish(next: Boot) {
  boot = next;
  subs.forEach((f) => f());
}

/** The language is the user's once the welcome step is saved; before that the server's value is only its default and
 *  the app keeps the system's. A later change made elsewhere (another window) follows through. */
function adopt(s: SetupState) {
  const chosen = s.complete || s.steps.some((x) => x.id === "welcome" && x.done);
  if (chosen && s.language !== serverLang) {
    serverLang = s.language;
    setLang(s.language);
  }
  // mentions of a user group in Slack text read as its handle (lib.ts, slackToMd)
  site.groups = Object.fromEntries(s.slack.usergroups.map((g) => [g.id, `@${g.handle.replace(/^@/, "")}`]));
}

/** Puts a fresh state in place (after a save answered with one, or a poll). */
export function putSetup(s: SetupState) {
  adopt(s);
  if (boot.phase === "ready" && JSON.stringify(boot.state) === JSON.stringify(s)) return; // same answer: nothing re-renders
  publish({ phase: "ready", state: s, legacy: false });
}

/** Asks the worker where setup stands. Never throws: the answer is the phase. */
export async function loadSetup(): Promise<Boot> {
  try {
    putSetup(await api.state());
  } catch (e) {
    if (e instanceof SetupError && e.status === 404) {
      if (serverLang === null) setLang("es"); // installs from before the setup API were Spanish only
      serverLang = "es";
      publish({ phase: "ready", state: null, legacy: true });
    } else if (boot.phase !== "ready") publish({ phase: "down" });
    // already running: a missed refresh keeps the last good state, like every other poll
  }
  return boot;
}

/** Fresh state after a step saved. Throws when the worker does not answer, so the step can say so. */
export async function refreshSetup(): Promise<SetupState> {
  const s = await api.state();
  putSetup(s);
  return s;
}

/** The state as it is this instant, for a decision made right after a save (a render may not have caught up yet). */
export function currentSetup(): SetupState | null {
  return boot.phase === "ready" ? boot.state : null;
}

export function useBoot(): Boot {
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => boot,
  );
}

/** Whether the app may say Clea reads production: only an install with a telemetry source does. A worker that
 *  predates setup (no state) is the first install, which has one. */
export function seesProduction(): boolean {
  return boot.phase === "ready" && boot.state ? boot.state.sees_production !== false : true;
}

/** The user's first name for the greeting, "" when setup has none. */
export function firstName(): string {
  const name = boot.phase === "ready" ? (boot.state?.user.name ?? "") : "";
  return name.trim().split(/\s+/)[0] ?? "";
}

/** True when the model in use draws from the user's ChatGPT plan: OpenAI asks that this be said where requests start. */
export function onChatGptPlan(): boolean {
  return boot.phase === "ready" && boot.state?.provider.auth === "siwc";
}

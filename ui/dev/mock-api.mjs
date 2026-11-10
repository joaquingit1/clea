// A stand-in for the worker's API, for building the onboarding and settings without a worker: `pnpm dev:mock` runs it
// on 4899 with vite pointed at it. Node only, no dependencies, everything in memory. It follows the setup contract
// closely enough to walk the whole flow, including the parts that go wrong:
//   - a sign-in that waits a few seconds and then finishes (Claude Code, ChatGPT); Codex is not installed at first,
//     and once it is, its first sign-in fails and the second works;
//   - an API key the vendor refuses (any key containing "bad", or with the wrong prefix);
//   - a Slack token short of scopes (a token containing "missing"), and one Slack refuses (no xoxp- prefix);
//   - a Jira site, token or JQL that is refused (containing "bad");
//   - the restart after "Start Clea": a few seconds of no answer at all, then complete and running.
// It also serves a small board (threads, PRs, chats, activity) so the app's own screens load after setup.
//
// Not part of the contract, for driving it by hand or from a test:
//   GET /api/_mock/reset?preset=fresh|complete&lang=en|es&gh=ok|missing|signedout&codex=0|1&down=0|1&legacy=0|1
//   GET /api/_mock/set?gh=...&codex=...&down=...&legacy=...   (one thing changed while the app is open)
// down=1 answers nothing at all (the worker is not running); legacy=1 answers 404 to the setup routes (a worker from
// before this API). The same options as environment variables set the state it starts in: MOCK_PRESET, MOCK_LANG,
// MOCK_GH, MOCK_CODEX.
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const PORT = Number(process.env.MOCK_PORT || 4899);
const VITE_PORT = Number(process.env.MOCK_VITE_PORT || 5199);
const now = () => Math.floor(Date.now() / 1000);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---- the workspace the mock pretends to be in --------------------------------------------------------------------- */

const CHANNELS = [
  ["C01", "team-payments", false, true, 14, "The payments team: questions, standups, decisions"],
  ["C02", "payments-prs", false, true, 22, "Pull requests waiting for review"],
  ["C03", "alerts-payments", false, true, 9, "Monitoring alerts for checkout and billing"],
  ["C04", "engineering", false, true, 187, "Everyone who writes code"],
  ["C05", "incidents", false, true, 96, "Live incidents and postmortems"],
  ["C06", "support-escalations", false, true, 41, "Support asks engineering for help"],
  ["C07", "payments-leads", true, true, 5, "Planning and staffing"],
  ["C08", "general", false, true, 312, "Company announcements"],
  ["C09", "deploys", false, true, 120, "Every deploy, posted by the bot"],
  ["C10", "random", false, true, 301, ""],
  ["C11", "team-growth", false, false, 11, "The growth team"],
  ["C12", "team-platform", false, false, 17, "The platform team"],
  ["C13", "platform-prs", false, false, 19, "Platform pull requests"],
  ["C14", "design-reviews", false, false, 28, "Weekly design critique"],
  ["C15", "security", true, false, 8, "Security reports"],
  ["C16", "data-questions", false, false, 64, "Ask the data team"],
  ["C17", "alerts-platform", false, false, 12, "Platform monitoring"],
  ["C18", "a-very-long-channel-name-for-the-quarterly-cross-functional-planning-working-group", false, false, 33, "A purpose long enough to need cutting off before it runs into the edge of the list on a narrow window"],
].map(([id, name, priv, member, members, purpose]) => ({ id, name, private: priv, member, members, purpose }));

const GROUPS = [
  { id: "S01", handle: "payments", name: "Payments team", members: 6, mine: true },
  { id: "S02", handle: "backend", name: "Backend engineers", members: 31, mine: true },
  { id: "S03", handle: "oncall", name: "On call this week", members: 4, mine: false },
  { id: "S04", handle: "frontend", name: "Frontend engineers", members: 24, mine: false },
];

const USERS = [
  ["U01", "grace", "Grace Hopper", "Staff engineer"],
  ["U02", "linus", "Linus Pauling", "Engineer"],
  ["U03", "katherine", "Katherine Johnson", "Engineering manager"],
  ["U04", "alan", "Alan Kay", "Engineer"],
  ["U05", "margaret", "Margaret Hamilton", "Principal engineer"],
  ["U06", "dennis", "Dennis Ritchie", "Engineer"],
  ["U07", "barbara", "Barbara Liskov", "Product designer"],
  ["U08", "edsger", "Edsger Dijkstra", "Engineer"],
  ["U09", "radia", "Radia Perlman", "Site reliability"],
  ["U10", "tim", "Tim Paterson", "Support lead"],
].map(([id, name, real_name, title]) => ({ id, name, real_name, title }));

const SUGGESTED = [
  { slack_id: "U01", name: "Grace Hopper", github: "ghopper" },
  { slack_id: "U02", name: "Linus Pauling", github: "" },
  { slack_id: "U03", name: "Katherine Johnson", github: "kjohnson" },
  { slack_id: "U04", name: "Alan Kay", github: "alankay" },
];

const ORGS = [
  { login: "acme", name: "Acme" },
  { login: "acme-labs", name: "Acme Labs" },
];
const repo = (org, name, language, days, local, priv = true, branch = "main") => ({
  name,
  full_name: `${org}/${name}`,
  default: branch,
  private: priv,
  pushed_at: new Date(Date.now() - days * 86400000).toISOString(),
  path: local ? `/Users/ada/code/${name}` : "",
  language,
});
const REPOS = {
  ada: [repo("ada", "dotfiles", "Shell", 3, true, false), repo("ada", "advent-of-code", "Rust", 320, false, false), repo("ada", "notes", "", 40, false)],
  acme: [
    repo("acme", "checkout", "TypeScript", 0, true),
    repo("acme", "billing", "Python", 0, true),
    repo("acme", "web", "TypeScript", 1, false),
    repo("acme", "mobile", "Kotlin", 2, false),
    repo("acme", "infra", "HCL", 2, false, true, "master"),
    repo("acme", "design-system", "TypeScript", 6, false),
    repo("acme", "docs", "MDX", 9, false, false),
    repo("acme", "payments-gateway-integration-tests-and-fixtures", "Python", 14, false),
    repo("acme", "analytics", "SQL", 30, false),
    repo("acme", "legacy-admin", "PHP", 410, false, true, "trunk"),
  ],
  "acme-labs": [repo("acme-labs", "experiments", "Python", 5, false), repo("acme-labs", "prototype-agent", "TypeScript", 12, false)],
};

const JIRA_PROJECTS = [
  { key: "PAY", name: "Payments" },
  { key: "WEB", name: "Web app" },
  { key: "MOB", name: "Mobile" },
  { key: "OPS", name: "Operations" },
];
const TICKETS = [
  { key: "PAY-812", summary: "Refunds over the card limit fail without a message", status: "In progress" },
  { key: "PAY-799", summary: "Invoice PDF shows the wrong tax id for EU customers", status: "To do" },
  { key: "WEB-2104", summary: "Checkout button stays disabled after fixing an invalid card", status: "To do" },
  { key: "OPS-311", summary: "Rotate the webhook signing secret", status: "In review" },
  { key: "MOB-455", summary: "Apple Pay sheet closes on the first tap", status: "To do" },
];

/* ---- words the worker would write, in the install's language ------------------------------------------------------ */

const TEXT = {
  en: {
    needName: "Enter your name.",
    unknown: "Unknown model provider.",
    keyNeeded: "An API key is needed the first time.",
    anthropicBad: "Anthropic refused this key: invalid x-api-key.",
    openaiBad: "OpenAI refused this key: Incorrect API key provided.",
    notReady: "This model is not ready yet.",
    detail: {
      install: (cli) => `${cli} is not installed on this Mac.`,
      login: (cli) => `${cli} is installed but not signed in.`,
      loginChatgpt: "Not signed in yet.",
      key: "No key saved yet.",
      ready: (cli, v) => `${cli} ${v} is signed in.`,
      readyKey: "The key was checked just now.",
    },
    loginFailed: "Codex closed before the sign-in finished. Try again.",
    slackBad: "Slack did not accept this token: invalid_auth.",
    slackAppBad: "Slack did not accept the App-Level Token: invalid_auth.",
    slackNoTeam: "Add at least one channel with the team role.",
    slackFirst: "Connect Slack first.",
    repoNeeded: "Pick at least one repository.",
    areaNeeded: "Describe what you and your team own.",
    jiraSite: "Clea could not reach that Jira site. Check the address.",
    jiraAuth: "Jira refused the email or token (401).",
    jiraFirst: "Connect Jira first.",
    jqlBad: "Jira could not read the extra JQL: expecting an operator before \"bad\".",
    levelBad: "Unknown level.",
    missing: "Some steps are not finished.",
    sum: {
      model: (p) => `${p.name}${p.plan ? ` · ${p.plan}` : ""}`,
      slack: (s) => `${s.workspace.name} · ${s.channels.length} ${s.channels.length === 1 ? "channel" : "channels"}`,
      code: (g) => `${g.org} · ${g.repos.length} ${g.repos.length === 1 ? "repository" : "repositories"}`,
      team: (t) => `${t.members.length} ${t.members.length === 1 ? "teammate" : "teammates"} · ${t.area}`,
      autonomy: { watch: "Watch", ask: "Ask first", auto: "Autonomous" },
      lang: "English",
    },
    names: { "claude-subscription": "Claude Code", "claude-api": "Anthropic API key", "openai-chatgpt": "ChatGPT", "codex-login": "Codex", "openai-api": "OpenAI API key" },
    greeting: () => (new Date().getHours() < 12 ? "Good morning" : new Date().getHours() < 19 ? "Good afternoon" : "Good evening"),
  },
  es: {
    needName: "Escribí tu nombre.",
    unknown: "Proveedor de modelo desconocido.",
    keyNeeded: "La primera vez hace falta una API key.",
    anthropicBad: "Anthropic rechazó esta key: invalid x-api-key.",
    openaiBad: "OpenAI rechazó esta key: Incorrect API key provided.",
    notReady: "Este modelo todavía no está listo.",
    detail: {
      install: (cli) => `${cli} no está instalado en esta Mac.`,
      login: (cli) => `${cli} está instalado pero sin sesión iniciada.`,
      loginChatgpt: "Todavía sin sesión iniciada.",
      key: "Todavía no hay una key guardada.",
      ready: (cli, v) => `${cli} ${v} tiene la sesión iniciada.`,
      readyKey: "La key se comprobó recién.",
    },
    loginFailed: "Codex se cerró antes de terminar el inicio de sesión. Probá de nuevo.",
    slackBad: "Slack no aceptó este token: invalid_auth.",
    slackAppBad: "Slack no aceptó el App-Level Token: invalid_auth.",
    slackNoTeam: "Agregá al menos un canal con el rol de equipo.",
    slackFirst: "Primero conectá Slack.",
    repoNeeded: "Elegí al menos un repositorio.",
    areaNeeded: "Describí de qué se ocupan vos y tu equipo.",
    jiraSite: "Clea no pudo llegar a ese sitio de Jira. Revisá la dirección.",
    jiraAuth: "Jira rechazó el email o el token (401).",
    jiraFirst: "Primero conectá Jira.",
    jqlBad: "Jira no pudo leer el JQL adicional: falta un operador antes de \"bad\".",
    levelBad: "Nivel desconocido.",
    missing: "Hay pasos sin terminar.",
    sum: {
      model: (p) => `${p.name}${p.plan ? ` · ${p.plan}` : ""}`,
      slack: (s) => `${s.workspace.name} · ${s.channels.length} ${s.channels.length === 1 ? "canal" : "canales"}`,
      code: (g) => `${g.org} · ${g.repos.length} ${g.repos.length === 1 ? "repositorio" : "repositorios"}`,
      team: (t) => `${t.members.length} ${t.members.length === 1 ? "compañero" : "compañeros"} · ${t.area}`,
      autonomy: { watch: "Observar", ask: "Preguntar primero", auto: "Autónomo" },
      lang: "Español",
    },
    names: { "claude-subscription": "Claude Code", "claude-api": "API key de Anthropic", "openai-chatgpt": "ChatGPT", "codex-login": "Codex", "openai-api": "API key de OpenAI" },
    greeting: () => (new Date().getHours() < 12 ? "Buenos días" : new Date().getHours() < 19 ? "Buenas tardes" : "Buenas noches"),
  },
};

/* ---- state -------------------------------------------------------------------------------------------------------- */

let S;
function reset({ preset = "fresh", lang = "en", gh = "ok", codex = "0", down = "0", legacy = "0" } = {}) {
  S = {
    down: down === "1", // answer nothing at all, as when the worker is not running
    legacy: legacy === "1", // no setup routes, as on a worker from before them
    downUntil: 0,
    runningAt: 0, // after finish: when the "restart" is over
    edition: "release",
    complete: false,
    language: lang === "es" ? "es" : "en",
    name: "",
    selected: null,
    signedIn: { "claude-subscription": false, "openai-chatgpt": false, "codex-login": false },
    keys: { "claude-api": "", "openai-api": "" },
    codexInstalled: codex === "1",
    codexTries: 0,
    login: { id: null, state: "idle", url: null, error: null, doneAt: 0 },
    slack: { connected: false, workspace: null, user: null, missing_scopes: [], socket: false, channels: [], usergroups: [], keywords: [], scan_dms: true, mentions: true },
    gh, // ok | missing | signedout
    github: { org: "", repos: [] },
    team: { members: [], area: "", keywords: [] },
    jira: { enabled: false, connected: false, base: "", email: "", account: "", projects: [], assigned: true, mentions: true, jql: "" },
    autonomy: "ask",
    autonomySet: false,
    chats: {},
  };
  if (preset === "complete") {
    const es = S.language === "es";
    Object.assign(S, { complete: true, name: "Ada Lovelace", selected: "claude-subscription", autonomySet: true });
    S.signedIn["claude-subscription"] = true;
    S.slack = {
      ...S.slack,
      connected: true,
      workspace: { name: "Acme", url: "https://acme.slack.com", team_id: "T01" },
      user: { id: "U00", name: "ada" },
      socket: true,
      channels: [
        { id: "C01", name: "team-payments", role: "team" },
        { id: "C02", name: "payments-prs", role: "prs" },
        { id: "C03", name: "alerts-payments", role: "alerts" },
      ],
      usergroups: [{ id: "S01", handle: "payments" }],
      keywords: ["checkout", "billing"],
    };
    S.github = { org: "acme", repos: [REPOS.acme[0], REPOS.acme[1]].map(({ name, full_name, path: p, default: d }) => ({ name, full_name, path: p, default: d })) };
    S.team = { members: SUGGESTED.slice(0, 3), area: es ? "los flujos de checkout y facturación" : "the checkout and billing flows", keywords: ["refund", "invoice"] };
    S.jira = { enabled: true, connected: true, base: "https://acme.atlassian.net", email: "ada@acme.com", account: "Ada Lovelace", projects: ["PAY"], assigned: true, mentions: true, jql: "" };
  }
}
reset({ preset: process.env.MOCK_PRESET, lang: process.env.MOCK_LANG, gh: process.env.MOCK_GH, codex: process.env.MOCK_CODEX });

const T = () => TEXT[S.language];
const hint = (k) => `${k.slice(0, k.startsWith("sk-ant-") ? 7 : 3)}…${k.slice(-4)}`;

function providers() {
  const d = T().detail;
  const cli = (name, installed) => ({
    name,
    installed,
    version: installed ? (name === "claude" ? "2.4.1" : "0.48.0") : "",
    install_url: name === "claude" ? "https://docs.claude.com/en/docs/claude-code/setup" : "https://developers.openai.com/codex/cli",
  });
  const sub = (id, vendor, engine, auth, name, plan, installed = true) => {
    const ready = installed && S.signedIn[id];
    const label = name === "claude" ? "Claude Code" : "Codex";
    return {
      id, vendor, engine, auth, cli: cli(name, installed), ready, selected: S.selected === id,
      account: ready ? "ada@acme.com" : "",
      plan: ready ? plan : "",
      needs: !installed ? "install" : ready ? null : "login",
      detail: !installed ? d.install(label) : ready ? (auth === "siwc" ? "" : d.ready(label, cli(name, true).version)) : auth === "siwc" ? d.loginChatgpt : d.login(label),
      expires_at: ready && auth === "siwc" ? now() + 20 * 86400 : null,
    };
  };
  const key = (id, vendor, engine, name) => {
    const k = S.keys[id];
    return { id, vendor, engine, auth: "api_key", cli: cli(name, true), ready: !!k, selected: S.selected === id, account: k ? hint(k) : "", plan: k ? "API" : "", needs: k ? null : "key", detail: k ? d.readyKey : d.key, expires_at: null };
  };
  return [
    sub("claude-subscription", "Anthropic", "claude", "subscription", "claude", "max"),
    key("claude-api", "Anthropic", "claude", "claude"),
    // Sign in with ChatGPT needs no CLI of the user's: the engine ships with Clea
    sub("openai-chatgpt", "OpenAI", "codex", "siwc", "codex", "plus"),
    sub("codex-login", "OpenAI", "codex", "subscription", "codex", "plus", S.codexInstalled),
    key("openai-api", "OpenAI", "codex", "codex"),
  ];
}

function state() {
  const ps = providers();
  const sel = ps.find((p) => p.selected && p.ready);
  const t = T();
  const jiraOn = S.jira.enabled && S.jira.connected && S.jira.projects.length > 0;
  const steps = [
    { id: "welcome", done: !!S.name, optional: false, summary: S.name ? `${S.name} · ${t.sum.lang}` : "" },
    { id: "model", done: !!sel, optional: false, summary: sel ? t.sum.model({ name: t.names[sel.id], plan: sel.plan }) : "" },
    { id: "slack", done: S.slack.connected && S.slack.channels.some((c) => c.role === "team"), optional: false, summary: "" },
    { id: "code", done: S.github.repos.length > 0, optional: false, summary: "" },
    { id: "team", done: !!S.team.area, optional: false, summary: "" },
    { id: "tickets", done: jiraOn, optional: true, summary: jiraOn ? S.jira.projects.join(", ") : "" },
    { id: "autonomy", done: S.autonomySet, optional: false, summary: S.autonomySet ? t.sum.autonomy[S.autonomy] : "" },
  ];
  if (steps[2].done) steps[2].summary = t.sum.slack(S.slack);
  if (steps[3].done) steps[3].summary = t.sum.code(S.github);
  if (steps[4].done) steps[4].summary = t.sum.team(S.team);
  return {
    edition: S.edition,
    complete: S.complete,
    running: S.complete && Date.now() >= S.runningAt,
    language: S.language,
    user: { name: S.name },
    steps,
    provider: sel ? { engine: sel.engine, auth: sel.auth } : { engine: "", auth: "" },
    providers: ps,
    slack: { ...S.slack, manifest_url: "https://api.slack.com/apps?new_app=1&manifest_json=%7B%22display_information%22%3A%7B%22name%22%3A%22Clea%22%7D%7D" },
    github: { cli: { installed: S.gh !== "missing", version: S.gh !== "missing" ? "2.83.0" : "" }, connected: S.gh === "ok", login: S.gh === "ok" ? "ada" : "", org: S.github.org, repos: S.github.repos },
    team: S.team,
    jira: S.jira,
    autonomy: S.autonomy,
  };
}

function loginState() {
  const l = S.login;
  if (l.state === "waiting" && Date.now() >= l.doneAt) {
    if (l.id === "codex-login" && S.codexTries === 1) Object.assign(l, { state: "error", error: T().loginFailed });
    else {
      S.signedIn[l.id] = true;
      l.state = "done";
    }
  }
  return { id: l.id, state: l.state, url: l.url, error: l.error };
}

/* ---- routes ------------------------------------------------------------------------------------------------------- */

class Refused extends Error {
  constructor(message, extra = {}) {
    super(message);
    this.extra = extra;
  }
}
const no = (message, extra) => {
  throw new Refused(message, extra);
};

const routes = {
  "GET /api/setup": () => state(),
  "POST /api/setup/profile": ({ name, language }) => {
    if (!String(name || "").trim()) no(T().needName);
    S.name = String(name).trim();
    if (language === "en" || language === "es") S.language = language;
    return { ok: true };
  },
  "POST /api/setup/provider": ({ id, api_key }) => {
    const p = providers().find((x) => x.id === id);
    if (!p) no(T().unknown);
    if (p.auth === "api_key") {
      if (api_key) {
        const k = String(api_key);
        const good = id === "claude-api" ? /^sk-ant-\S{6,}$/.test(k) : /^sk-\S{6,}$/.test(k) && !k.startsWith("sk-ant-");
        if (!good || k.includes("bad")) no(id === "claude-api" ? T().anthropicBad : T().openaiBad);
        S.keys[id] = k;
      } else if (!S.keys[id]) no(T().keyNeeded);
    } else if (!p.ready) no(T().notReady);
    S.selected = id;
    return { ok: true, provider: providers().find((x) => x.id === id) };
  },
  "POST /api/setup/provider/login": ({ id }) => {
    const p = providers().find((x) => x.id === id);
    if (!p || p.auth === "api_key") no(T().unknown);
    if (id === "codex-login") S.codexTries++;
    S.login = { id, state: "waiting", url: id === "openai-chatgpt" ? "https://auth.openai.com/oauth/authorize?client_id=mock" : id === "claude-subscription" ? "https://claude.ai/oauth/authorize?mock=1" : null, error: null, doneAt: Date.now() + 4500 };
    return loginState();
  },
  "GET /api/setup/provider/login": () => loginState(),
  "POST /api/setup/provider/logout": ({ id }) => {
    if (id in S.keys) S.keys[id] = "";
    else if (id === "openai-chatgpt") S.signedIn[id] = false;
    else no(T().unknown); // the user's own Claude Code or Codex login is never touched
    if (S.selected === id) S.selected = null;
    return { ok: true };
  },

  "POST /api/setup/slack/connect": ({ user_token, app_token }) => {
    const u = String(user_token || "");
    if (!u.startsWith("xoxp-") || u.includes("bad")) no(T().slackBad);
    if (app_token && (!String(app_token).startsWith("xapp-") || String(app_token).includes("bad"))) no(T().slackAppBad);
    Object.assign(S.slack, {
      connected: true,
      workspace: { name: "Acme", url: "https://acme.slack.com", team_id: "T01" },
      user: { id: "U00", name: "ada" },
      missing_scopes: u.includes("missing") ? ["search:read", "usergroups:read", "im:history"] : [],
      socket: !!app_token,
    });
    return { ok: true, slack: state().slack };
  },
  "GET /api/setup/slack/channels": (_b, q) => {
    if (!S.slack.connected) no(T().slackFirst);
    const s = (q.get("q") || "").toLowerCase();
    return CHANNELS.filter((c) => c.name.includes(s)).sort((a, b) => Number(b.member) - Number(a.member));
  },
  "GET /api/setup/slack/usergroups": () => (S.slack.connected ? GROUPS : no(T().slackFirst)),
  "GET /api/setup/slack/users": (_b, q) => {
    const s = (q.get("q") || "").toLowerCase();
    return USERS.filter((u) => `${u.name} ${u.real_name}`.toLowerCase().includes(s)).slice(0, 50);
  },
  "POST /api/setup/slack": ({ channels = [], usergroups = [], keywords = [], scan_dms, mentions }) => {
    if (!S.slack.connected) no(T().slackFirst);
    if (!channels.some((c) => c.role === "team")) no(T().slackNoTeam);
    Object.assign(S.slack, { channels, usergroups, keywords, scan_dms: !!scan_dms, mentions: !!mentions });
    return { ok: true, slack: state().slack };
  },

  "GET /api/setup/github/orgs": () => ({ login: "ada", orgs: ORGS }),
  "GET /api/setup/github/repos": (_b, q) => {
    const s = (q.get("q") || "").toLowerCase();
    return (REPOS[q.get("org")] || []).filter((r) => r.name.toLowerCase().includes(s));
  },
  "POST /api/setup/github": ({ org, repos = [] }) => {
    if (!repos.length) no(T().repoNeeded);
    S.github = { org: String(org || ""), repos };
    return { ok: true, github: state().github };
  },

  "GET /api/setup/team/suggest": () => ({ members: S.slack.connected ? SUGGESTED : [] }),
  "POST /api/setup/team": ({ members = [], area, keywords = [] }) => {
    if (!String(area || "").trim()) no(T().areaNeeded);
    S.team = { members, area: String(area).trim(), keywords };
    return { ok: true, team: S.team };
  },

  "POST /api/setup/jira/connect": ({ base, email, token }) => {
    if (!/atlassian\.net/.test(String(base)) || String(base).includes("bad")) no(T().jiraSite);
    if (String(token).includes("bad")) no(T().jiraAuth);
    Object.assign(S.jira, { connected: true, base: String(base).replace(/\/+$/, ""), email: String(email), account: S.name || "Ada Lovelace" });
    return { ok: true, jira: S.jira, projects: JIRA_PROJECTS };
  },
  "POST /api/setup/jira": ({ enabled, projects = [], assigned, mentions, jql = "" }) => {
    if (!S.jira.connected) no(T().jiraFirst);
    if (String(jql).includes("bad")) no(T().jqlBad);
    Object.assign(S.jira, { enabled: !!enabled, projects, assigned: !!assigned, mentions: !!mentions, jql: String(jql) });
    return { ok: true, jira: S.jira };
  },
  "POST /api/setup/jira/preview": ({ projects = [], assigned, mentions, jql = "" }) => {
    if (String(jql).includes("bad")) no(T().jqlBad);
    const mine = TICKETS.filter((x) => projects.includes(x.key.split("-")[0]));
    const count = !assigned && !mentions ? 0 : mine.length * (assigned && mentions ? 4 : assigned ? 3 : 1) - (jql ? 1 : 0);
    return { count: Math.max(count, 0), sample: count > 0 ? mine.slice(0, 3) : [] };
  },

  "POST /api/setup/autonomy": ({ level }) => {
    if (!["watch", "ask", "auto"].includes(level)) no(T().levelBad);
    S.autonomy = level;
    S.autonomySet = true;
    return { ok: true };
  },
  "POST /api/setup/finish": () => {
    const missing = state().steps.filter((s) => !s.done && !s.optional).map((s) => s.id);
    if (missing.length) no(T().missing, { missing });
    S.complete = true;
    // the restart: nothing answers for a moment, then the worker is back in normal mode
    S.downUntil = Date.now() + 2500;
    S.runningAt = Date.now() + 5500;
    return { ok: true, missing: [] };
  },
  "POST /api/setup/reopen": () => {
    S.complete = false;
    return { ok: true };
  },

  // ---- just enough of the app's own API for its screens to load
  "GET /api/config": () => ({ github_org: S.github.org, jira_base: S.jira.connected ? S.jira.base : "" }),
  "GET /api/board": () => board(),
  "GET /api/threads": () => threads(),
  "GET /api/chats": () => Object.entries(S.chats).map(([conv, c]) => ({ conv, title: c.messages[0]?.text.slice(0, 60) ?? "Chat", at: c.at, item: null, n: c.messages.length, thinking: false })),
  "GET /api/activity": () => activity(),
  "GET /api/notifications": () => [],
  "POST /api/chat": ({ text, conv }) => {
    const id = conv || `c${Object.keys(S.chats).length + 1}`;
    const c = (S.chats[id] ||= { messages: [], at: now() });
    const es = S.language === "es";
    c.messages.push({ id: c.messages.length + 1, role: "user", text: String(text), at: now(), meta: {} });
    c.messages.push({ id: c.messages.length + 1, role: "agent", text: es ? "Esto es el mock de desarrollo: acá respondería Clea." : "This is the development mock: Clea would answer here.", at: now(), meta: { secs: 4, fast: true } });
    c.at = now();
    return { ok: true, conv: id };
  },
};

function threads() {
  const es = S.language === "es";
  const t = now();
  const mk = (id, phase, kind, en, esText, extra = {}) => ({
    id, kind, status: phase, stage: extra.stage ?? (es ? "En curso" : "In progress"), phase, headline: es ? esText : en,
    origin: extra.origin ?? "#team-payments", ticket: extra.ticket ?? null, need: extra.need ?? "", last: extra.last ?? "", updated: t - (extra.ago ?? 600), created: t - (extra.ago ?? 600) - 3600,
    urgency: 1, prs: extra.prs ?? [], asks: phase === "you" ? 1 : 0, next: null,
  });
  return [
    mk(101, "you", "Support", "Refunds over the card limit fail without a message", "Los reembolsos sobre el límite de la tarjeta fallan sin mensaje", {
      ticket: "PAY-812", ago: 900,
      need: es ? "Aprobá la respuesta para Grace en el hilo" : "Approve the reply to Grace in the thread",
      prs: [{ url: "https://github.com/acme/billing/pull/412", label: "billing#412", ci: "green", approved: true, linked: true }],
    }),
    mk(102, "you", "Review", "Review of checkout#977: retry the payment intent once", "Review de checkout#977: reintentar el payment intent una vez", { origin: "#payments-prs", ago: 5400, need: es ? "Dos comentarios listos para mandar" : "Two comments ready to send" }),
    mk(103, "running", "Fix", "Invoice PDF shows the wrong tax id for EU customers", "El PDF de la factura muestra mal el tax id para clientes de la UE", { ticket: "PAY-799", ago: 120, last: es ? "Reproduciendo el bug con un test" : "Reproducing the bug with a test" }),
    mk(104, "waiting", "Fix", "Checkout button stays disabled after fixing an invalid card", "El botón de checkout queda deshabilitado después de corregir una tarjeta inválida", { ticket: "WEB-2104", ago: 7200, last: es ? "Esperando el CI" : "Waiting for CI" }),
    mk(105, "done", "Answer", "Which webhook events do we retry?", "¿Qué eventos de webhook reintentamos?", { ago: 20000, last: es ? "Hecho: respondido en el hilo" : "Done: answered in the thread" }),
    mk(106, "done", "Fix", "Rotate the webhook signing secret", "Rotar el secreto de firma del webhook", { ticket: "OPS-311", ago: 90000, last: es ? "Hecho: mergeado" : "Done: merged" }),
  ];
}

function board() {
  const es = S.language === "es";
  const ts = threads();
  const pr = (repoName, number, state, title, why, extra = {}) => ({
    repo: repoName, number, head: "abc1234", approval: extra.approval ?? null, ticket: extra.ticket ?? null, repro: null, label: `${repoName}#${number}`,
    url: `https://github.com/acme/${repoName}/pull/${number}`, title, state, why, after: extra.after ?? [], created: new Date(Date.now() - (extra.days ?? 1) * 86400000).toISOString(),
    case: extra.case ?? null, ci: extra.ci ?? "green", threads: 0, updated: now() - 3000,
  });
  return {
    at: now(),
    greeting: T().greeting(),
    needs: ts.filter((x) => x.phase === "you"),
    running: ts.filter((x) => x.phase === "running"),
    waiting: ts.filter((x) => x.phase === "waiting"),
    shipped: ts.filter((x) => x.phase === "done"),
    dropped: [],
    prs: [
      pr("billing", 412, "merge", es ? "mostrar el motivo cuando un reembolso supera el límite" : "show the reason when a refund is over the limit", es ? "Verde y aprobado por Grace" : "Green and approved by Grace", { approval: "human", ticket: "PAY-812", case: 101 }),
      pr("checkout", 981, "decide", es ? "reintentar el payment intent una vez" : "retry the payment intent once", es ? "Choca con checkout#977: decidí el orden" : "Conflicts with checkout#977: decide the order", { days: 3 }),
      pr("billing", 409, "queued", es ? "tax id por país en el PDF" : "tax id per country in the PDF", es ? "Aprobado, va después de billing#412" : "Approved, goes after billing#412", { after: ["billing#412"], approval: "human", days: 2 }),
      pr("checkout", 975, "review", es ? "habilitar el botón al corregir la tarjeta" : "enable the button once the card is fixed", es ? "Verde, espera un approve" : "Green, waiting for an approval", { days: 5 }),
      pr("billing", 405, "working", es ? "rotación del secreto del webhook" : "webhook secret rotation", es ? "CI corriendo" : "CI running", { ci: "running", days: 16 }),
    ],
    blocking: [],
    stats: { prs_week: 7, done_week: 12, cost_today: 18.4, open: 4 },
    health: { rev: "296629a0", paused_until: null, wrong_account: null, worker_beat: now() },
  };
}

function activity() {
  const es = S.language === "es";
  const t = now();
  const a = (id, mins, stage, en, esTitle, headline, note = "") => ({ id, at: t - mins * 60, stage, title: es ? esTitle : en, headline, note, kind: "Fix" });
  const ts = threads();
  return [
    a(103, 2, "repro", "Reproducing", "Reproduciendo", ts[2].headline, es ? "El test falla sin el fix" : "The test fails without the fix"),
    a(103, 14, "investigate", "Investigating", "Investigando", ts[2].headline),
    a(101, 15, "awaiting_approval", "Waiting for your OK", "Esperando tu ok", ts[0].headline, es ? "Respuesta lista para Grace" : "Reply ready for Grace"),
    a(104, 120, "babysit", "Waiting for CI", "Esperando el CI", ts[3].headline),
    a(105, 330, "done", "Answered", "Respondido", ts[4].headline),
    a(106, 1500, "done", "Merged", "Mergeado", ts[5].headline),
  ];
}

function detail(id) {
  const th = threads().find((x) => x.id === id);
  if (!th) return null;
  const es = S.language === "es";
  return {
    ...th,
    bullets: es ? ["El límite se valida en el gateway, no en billing", "El error llega sin cuerpo y la UI no muestra nada"] : ["The limit is checked in the gateway, not in billing", "The error arrives without a body, so the UI shows nothing"],
    analysis: es ? "El gateway responde 422 sin cuerpo cuando el reembolso supera el límite de la tarjeta." : "The gateway answers 422 with no body when the refund is over the card's limit.",
    cause: "probado",
    technical: null,
    source: { text: es ? "¿Alguien sabe por qué fallan los reembolsos grandes?" : "Does anyone know why large refunds fail?", channel: "team-payments", link: "https://acme.slack.com/archives/C01/p1", author: "Grace Hopper" },
    report: "",
    detail: "",
    asks: th.phase === "you" ? [{ id: 9000 + id, kind: "reply", what: es ? "Responder a Grace" : "Reply to Grace", text: es ? "Lo encontré: el gateway rechaza el reembolso y no devolvemos el motivo. Hay un PR con el fix." : "Found it: the gateway refuses the refund and we drop the reason. There is a PR with the fix.", comments: [], slack: null, at: now() - 800, expired: false }] : [],
    repro: null,
    caveats: [],
    blocked: null,
    events: activity().filter((x) => x.id === id).map(({ at, stage, title, note }) => ({ at, stage, title, note })).reverse(),
    cost: 1.37,
    updates: [],
  };
}

/* ---- server ------------------------------------------------------------------------------------------------------- */

const send = (res, code, body) => {
  res.writeHead(code, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://mock");
  const key = `${req.method} ${url.pathname}`;
  if (url.pathname === "/api/_mock/reset") {
    reset(Object.fromEntries(url.searchParams));
    return send(res, 200, { ok: true, state: state() });
  }
  if (url.pathname === "/api/_mock/set") {
    // change one thing while the app is open: ?gh=ok, ?codex=1, ?down=1
    const q = url.searchParams;
    if (q.has("gh")) S.gh = q.get("gh");
    if (q.has("codex")) S.codexInstalled = q.get("codex") === "1";
    if (q.has("down")) S.down = q.get("down") === "1";
    if (q.has("legacy")) S.legacy = q.get("legacy") === "1";
    return send(res, 200, { ok: true });
  }
  if (S.down || Date.now() < S.downUntil) return req.socket.destroy(); // not running: no answer at all
  if (S.legacy && url.pathname.startsWith("/api/setup")) return send(res, 404, { error: "not found" });

  let body = {};
  if (req.method === "POST") {
    let raw = "";
    for await (const c of req) raw += c;
    try {
      body = raw ? JSON.parse(raw) : {};
    } catch {
      return send(res, 400, { error: "bad JSON" });
    }
    if (req.headers["x-ap-console"] !== "1") return send(res, 403, { error: "missing X-AP-Console" });
  }
  await wait(req.method === "POST" ? 450 : url.pathname.includes("/slack/channels") || url.pathname.includes("/github/repos") ? 280 : 90);

  try {
    let m;
    if (routes[key]) return send(res, 200, routes[key](body, url.searchParams));
    if ((m = url.pathname.match(/^\/api\/thread\/(\d+)$/))) {
      const d = detail(Number(m[1]));
      return d ? send(res, 200, d) : send(res, 404, { error: "not found" });
    }
    if ((m = url.pathname.match(/^\/api\/chat\/(.+)$/))) {
      const c = S.chats[decodeURIComponent(m[1])];
      return send(res, 200, { conv: m[1], messages: c?.messages ?? [], thinking: false, since: null, step: null });
    }
    return send(res, 404, { error: `the mock has no ${key}` });
  } catch (e) {
    if (e instanceof Refused) return send(res, 400, { error: e.message, ...e.extra });
    console.error(e);
    return send(res, 500, { error: String(e) });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`mock API on http://127.0.0.1:${PORT} (preset ${process.env.MOCK_PRESET || "fresh"}, ${S.language})`);
  if (!process.argv.includes("--with-vite")) return;
  // `pnpm dev:mock`: vite next to it, its /api proxy pointed here, on a port of its own so a `pnpm dev` against the
  // real worker can stay open
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const vite = spawn(path.join(root, "node_modules", ".bin", "vite"), ["--port", String(VITE_PORT), "--strictPort"], {
    cwd: root,
    stdio: "inherit",
    env: { ...process.env, CLEA_API: `http://127.0.0.1:${PORT}` },
  });
  const stop = () => {
    vite.kill();
    server.close();
    process.exit(0);
  };
  vite.on("exit", stop);
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
});

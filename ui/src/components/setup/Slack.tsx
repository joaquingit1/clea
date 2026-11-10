import { ArrowUpRight, Check, Hash, Lock, Plus, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { t, tn, type Key } from "../../i18n";
import { cx } from "../../lib";
import { api, errorText, refreshSetup, type ChannelChoice, type ChannelRole, type SetupState, type SlackChannel, type SlackGroup } from "../../setup";
import { BTN, Connected, Field, LoadFailed, Notice, Part, SearchInput, SecretInput, Select, Switch, TagInput, reveal, useDebounced, useLoad } from "./fields";
import { Invalid, StepFrame } from "./frame";

const ROLES: ChannelRole[] = ["team", "prs", "alerts", "watch"];

/** What stops working without a scope, for the ones Clea's manifest asks for. */
function scopeEffect(scope: string): Key {
  if (scope === "channels:history") return "slack.scope.history.public";
  if (scope === "groups:history") return "slack.scope.history.private";
  if (scope === "im:history") return "slack.scope.history.im";
  if (scope === "mpim:history") return "slack.scope.history.mpim";
  if (/^(channels|groups|im|mpim):read$/.test(scope)) return "slack.scope.list";
  if (scope.startsWith("users:") || scope.startsWith("users.profile:")) return "slack.scope.users";
  if (scope.startsWith("usergroups:")) return "slack.scope.usergroups";
  if (scope.startsWith("chat:")) return "slack.scope.write";
  if (scope.startsWith("search:")) return "slack.scope.search";
  if (scope.startsWith("reactions:")) return "slack.scope.reactions";
  if (scope.startsWith("files:")) return "slack.scope.files";
  if (scope.startsWith("pins:")) return "slack.scope.pins";
  return "slack.scope.other";
}

/** Slack: first the connection (create the app from Clea's manifest, install it, paste its tokens), then what to read:
 *  channels each with a role, the user's groups, keywords, and two switches. */
export function SlackStep({ state }: { state: SetupState }) {
  const s = state.slack;
  const [replacing, setReplacing] = useState(false);
  return !s.connected || replacing ? <Connect state={state} onDone={() => setReplacing(false)} onKeep={s.connected ? () => setReplacing(false) : undefined} /> : <Picker state={state} onReplace={() => setReplacing(true)} />;
}

function Connect({ state, onDone, onKeep }: { state: SetupState; onDone: () => void; onKeep?: () => void }) {
  const [user, setUser] = useState("");
  const [app, setApp] = useState("");
  // a check made here names its text by key, so it follows a language switch; the worker's refusal is already text
  const [bad, setBad] = useState<{ field: "user" | "app"; key?: Key; text?: string } | null>(null);
  const say = (f: "user" | "app") => (bad?.field === f ? (bad.key ? t(bad.key) : (bad.text ?? null)) : null);

  const connect = async () => {
    const u = user.trim(), a = app.trim();
    if (!u) return fail({ field: "user", key: "slack.err.userTokenEmpty" });
    if (!u.startsWith("xoxp-")) return fail({ field: "user", key: "slack.err.userToken" });
    if (a && !a.startsWith("xapp-")) return fail({ field: "app", key: "slack.err.appToken" });
    try {
      await api.slackConnect(u, a || undefined);
      await refreshSetup();
    } catch (e) {
      // the worker's reason belongs under the token it is about, and both tokens stay as typed
      return fail({ field: /xapp|app-level|app token/i.test(errorText(e)) ? "app" : "user", text: errorText(e) });
    }
    onDone();
    return "stay" as const; // connected: the same step now shows what to read
  };
  const fail = (b: NonNullable<typeof bad>): never => {
    setBad(b);
    throw new Invalid();
  };

  return (
    <StepFrame title={t("slack.title")} lead={t("slack.lead")} save={connect} submit={t("common.connect")}>
      <ol className="space-y-5">
        <Guide n={1} title={t("slack.guide.1.title")} body={t("slack.guide.1.body")}>
          <a href={state.slack.manifest_url} target="_blank" rel="noreferrer" className={cx(BTN.secondary, "mt-2.5")}>
            {t("slack.guide.1.action")} <ArrowUpRight size={13} className="opacity-60" />
          </a>
        </Guide>
        <Guide n={2} title={t("slack.guide.2.title")} body={t("slack.guide.2.body")} />
        <Guide n={3} title={t("slack.guide.3.title")} body={t("slack.guide.3.body")}>
          {/* where the token is, as the path Slack's own pages name it, and a way back to the app if its tab was closed */}
          <p className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[12px] text-fg-3">
            {["api.slack.com/apps", "Clea", "OAuth & Permissions", "OAuth Tokens"].map((x) => (
              <span key={x} className="contents">
                <span>{x}</span>
                <span aria-hidden>›</span>
              </span>
            ))}
            <span className="rounded-md bg-hover px-1.5 py-0.5 font-medium text-fg-2">User OAuth Token</span>
          </p>
          <a href="https://api.slack.com/apps" target="_blank" rel="noreferrer" className={cx(BTN.secondary, "mt-2.5")}>
            {t("slack.guide.3.action")} <ArrowUpRight size={13} className="opacity-60" />
          </a>
          <div className="mt-3 space-y-4">
            <Field label={t("slack.userToken")} error={say("user")} hint={t("slack.guide.3.kept")}>
              {(p) => (
                <SecretInput
                  {...p}
                  value={user}
                  placeholder="xoxp-…"
                  onChange={(e) => {
                    setUser(e.target.value);
                    setBad(null);
                  }}
                />
              )}
            </Field>
            <Field label={t("slack.appToken")} optional error={say("app")} hint={t("slack.appTokenHint")}>
              {(p) => (
                <SecretInput
                  {...p}
                  value={app}
                  placeholder="xapp-…"
                  onChange={(e) => {
                    setApp(e.target.value);
                    setBad(null);
                  }}
                />
              )}
            </Field>
          </div>
        </Guide>
      </ol>
      {onKeep ? (
        <button type="button" onClick={onKeep} className={BTN.link}>
          {t("slack.keepTokens")}
        </button>
      ) : null}
    </StepFrame>
  );
}

function Guide({ n, title, body, children }: { n: number; title: string; body: string; children?: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="mt-px grid size-6 shrink-0 place-items-center rounded-full bg-brand-soft text-[12px] font-semibold text-brand-ink tabular-nums">{n}</span>
      <div className="min-w-0 flex-1">
        <h2 className="text-[14px] leading-6 font-semibold text-fg">{title}</h2>
        <p className="text-[13px] leading-snug text-fg-2">{body}</p>
        {children}
      </div>
    </li>
  );
}

function Picker({ state, onReplace }: { state: SetupState; onReplace: () => void }) {
  const s = state.slack;
  const [channels, setChannels] = useState<ChannelChoice[]>(s.channels);
  const [groups, setGroups] = useState(s.usergroups);
  const [keywords, setKeywords] = useState(s.keywords);
  const [dms, setDms] = useState(s.scan_dms);
  const [mentions, setMentions] = useState(s.mentions);

  // the user's own groups start chosen the first time through; after that the saved choice stands, even an empty one
  const all = useLoad<SlackGroup[]>("groups", api.slackGroups);
  const fresh = !state.steps.find((x) => x.id === "slack")?.done && !s.usergroups.length;
  const seeded = useRef(false);
  useEffect(() => {
    if (!all.data || seeded.current) return;
    seeded.current = true;
    if (fresh) setGroups(all.data.filter((g) => g.mine).map(({ id, handle }) => ({ id, handle })));
  }, [all.data, fresh]);

  const hasTeam = channels.some((c) => c.role === "team");
  const body = { channels, usergroups: groups, keywords, scan_dms: dms, mentions };
  const saved = { channels: s.channels, usergroups: s.usergroups, keywords: s.keywords, scan_dms: s.scan_dms, mentions: s.mentions };
  return (
    <StepFrame
      title={t("slack.title")}
      lead={t("slack.lead")}
      save={async () => {
        await api.slack(body);
        await refreshSetup();
      }}
      ready={hasTeam}
      blocked={t("slack.needTeam")}
      dirty={JSON.stringify(body) !== JSON.stringify(saved)}
    >
      <div className="space-y-2.5">
        <Connected
          detail={t(s.socket ? "slack.socketOn" : "slack.socketOff")}
          action={
            <button type="button" onClick={onReplace} className={BTN.quiet}>
              {t("slack.replace")}
            </button>
          }
        >
          {t("slack.connected", { workspace: s.workspace?.name ?? "Slack", user: s.user?.name ?? "" })}
        </Connected>
        {s.missing_scopes.length ? (
          <Notice title={t("slack.missing.title")}>
            <p>{t("slack.missing.body")}</p>
            <ul className="mt-2 space-y-1">
              {s.missing_scopes.map((sc) => (
                <li key={sc} className="flex flex-wrap gap-x-2">
                  <code className="font-mono text-[12px] text-fg">{sc}</code>
                  <span>{t(scopeEffect(sc))}</span>
                </li>
              ))}
            </ul>
          </Notice>
        ) : null}
      </div>

      <Part title={t("slack.channels.title")} lead={t("slack.channels.lead")}>
        <Channels value={channels} onChange={setChannels} />
      </Part>

      <Part title={t("slack.groups.title")} lead={t("slack.groups.lead")}>
        {all.failed ? (
          <LoadFailed retry={all.retry}>{t("common.noAnswer")}</LoadFailed>
        ) : !all.data ? (
          <p className="shimmer text-[13px]">{t("common.loading")}</p>
        ) : all.data.length ? (
          <div className="flex flex-wrap gap-1.5">
            {all.data.map((g) => {
              const on = groups.some((x) => x.id === g.id);
              return (
                <button
                  key={g.id}
                  type="button"
                  aria-pressed={on}
                  title={g.name}
                  onClick={() => setGroups(on ? groups.filter((x) => x.id !== g.id) : [...groups, { id: g.id, handle: g.handle }])}
                  className={cx(
                    "inline-flex h-7 items-center gap-1 rounded-full px-2.5 text-[13px] transition",
                    on ? "bg-brand-soft text-brand-ink shadow-[0_0_0_1px_color-mix(in_oklab,var(--brand-ink)_45%,transparent)]" : "text-fg-2 shadow-[0_0_0_0.5px_var(--line-strong)] hover:bg-hover hover:text-fg",
                  )}
                >
                  {on ? <Check size={12} strokeWidth={2.6} /> : null}@{g.handle.replace(/^@/, "")}
                </button>
              );
            })}
          </div>
        ) : (
          <p className="text-[13px] text-fg-3">
            {t(s.missing_scopes.some((sc) => sc.startsWith("usergroups:")) ? "slack.groups.noScope" : "slack.groups.none")}
          </p>
        )}
      </Part>

      <Part title={t("slack.keywords.title")} lead={t("slack.keywords.lead")}>
        <TagInput value={keywords} onChange={setKeywords} placeholder={t("slack.keywords.placeholder")} label={t("slack.keywords.title")} />
      </Part>

      <div className="space-y-3.5">
        <Switch checked={dms} onChange={setDms} label={t("slack.dms")} hint={t("slack.dms.hint")} />
        <Switch checked={mentions} onChange={setMentions} label={t("slack.mentions")} hint={t("slack.mentions.hint")} />
      </div>
    </StepFrame>
  );
}

/** The channels Clea reads: the chosen ones on top, each with its role, and a search to add more. Adding one asks what
 *  it is for, with the four roles explained where the choice is made. */
function Channels({ value, onChange }: { value: ChannelChoice[]; onChange: (v: ChannelChoice[]) => void }) {
  const [q, setQ] = useState("");
  const [asking, setAsking] = useState<SlackChannel | null>(null);
  const text = useDebounced(q.trim(), 250);
  const found = useLoad<SlackChannel[]>(`channels:${text}`, () => api.slackChannels(text));
  const typing = q.trim() !== text;
  // the question opens with all four roles in view, not half under the footer
  const question = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (asking) reveal(question.current);
  }, [asking]);

  const add = (c: SlackChannel, role: ChannelRole) => {
    onChange([...value.filter((x) => x.id !== c.id), { id: c.id, name: c.name, role }]);
    setAsking(null);
  };
  return (
    <div className="space-y-2.5">
      {value.length ? (
        <ul className="space-y-1.5">
          {value.map((c) => (
            <li key={c.id} className="flex items-center gap-1.5 rounded-xl bg-[var(--field)] py-1 pr-1 pl-3 shadow-[0_0_0_0.5px_var(--line-strong)]">
              <Hash size={14} className="shrink-0 text-fg-3" />
              <span className="min-w-0 flex-1 truncate text-[14px] text-fg">{c.name}</span>
              <Select
                bare
                value={c.role}
                aria-label={t("slack.channels.role", { name: c.name })}
                onChange={(e) => onChange(value.map((x) => (x.id === c.id ? { ...x, role: e.target.value as ChannelRole } : x)))}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {t(`slack.role.${r}`)}
                  </option>
                ))}
              </Select>
              <button type="button" aria-label={t("slack.channels.remove", { name: c.name })} onClick={() => onChange(value.filter((x) => x.id !== c.id))} className="grid size-7 shrink-0 place-items-center rounded-lg text-fg-3 hover:bg-hover hover:text-fg">
                <X size={14} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <SearchInput
        value={q}
        onChange={(v) => {
          setQ(v);
          setAsking(null);
        }}
        placeholder={t("slack.channels.search")}
        busy={found.busy || typing}
      />
      {asking ? (
        // the question takes the list's place, so all four roles are in view at once
        <div
          ref={question}
          className="fade-in rounded-xl bg-hover p-3"
          onKeyDown={(e) => {
            if (e.key === "Escape") setAsking(null);
          }}
        >
          <div className="mb-2 flex items-center gap-2">
            <p className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg">{t("slack.channels.pickRole", { name: asking.name })}</p>
            <button type="button" aria-label={t("common.cancel")} onClick={() => setAsking(null)} className="grid size-6 shrink-0 place-items-center rounded-md text-fg-3 hover:bg-active hover:text-fg">
              <X size={14} />
            </button>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {ROLES.map((r, k) => (
              <button key={r} type="button" autoFocus={k === 0} onClick={() => add(asking, r)} className="choice rounded-xl px-3 py-2 text-left">
                <span className="block text-[13.5px] font-medium text-fg">{t(`slack.role.${r}`)}</span>
                <span className="mt-0.5 block text-[12.5px] leading-snug text-fg-2">{t(`slack.role.${r}.what`)}</span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="max-h-[232px] overflow-y-auto rounded-xl shadow-[0_0_0_0.5px_var(--line-strong)]">
          {found.failed ? (
            <LoadFailed retry={found.retry}>{t("slack.channels.failed")}</LoadFailed>
          ) : !found.data ? (
            <p className="shimmer px-3 py-3 text-[13px]">{t("common.loading")}</p>
          ) : !found.data.length ? (
            <p className="px-3 py-3 text-[13px] text-fg-3">{t("slack.channels.none")}</p>
          ) : (
            <ul>
              {found.data.map((c, i) => {
                const chosen = value.find((x) => x.id === c.id);
                return (
                  <li key={c.id} className={cx(i > 0 && "border-t-[0.5px] border-line")}>
                    <button type="button" disabled={!!chosen} onClick={() => setAsking(c)} className={cx("flex w-full items-center gap-2 px-3 py-2 text-left transition", !chosen && "hover:bg-hover")}>
                      {c.private ? <Lock size={13} className="shrink-0 text-fg-3" aria-label={t("slack.channels.private")} /> : <Hash size={14} className="shrink-0 text-fg-3" />}
                      <span className="min-w-0 flex-1">
                        <span className={cx("block truncate text-[14px]", chosen ? "text-fg-3" : "text-fg")}>{c.name}</span>
                        <span className="block truncate text-[12px] text-fg-3">
                          {[c.members ? tn("slack.channels.members", c.members) : "", c.member ? "" : t("slack.channels.notMember"), c.purpose].filter(Boolean).join(" · ")}
                        </span>
                      </span>
                      {chosen ? (
                        <span className="inline-flex shrink-0 items-center gap-1 text-[12.5px] text-fg-3">
                          <Check size={13} /> {t(`slack.role.${chosen.role}`)}
                        </span>
                      ) : (
                        <Plus size={15} className="shrink-0 text-fg-3" />
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

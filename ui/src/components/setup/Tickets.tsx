import { ArrowUpRight, Check, ChevronDown } from "lucide-react";
import { useState } from "react";
import { t, tn, type Key } from "../../i18n";
import { cx, loadSite } from "../../lib";
import { api, refreshSetup, type JiraPreview, type JiraProject, type SetupState } from "../../setup";
import { Spinner, Tag } from "../ui";
import { BTN, Connected, ErrorText, Field, Part, SecretInput, Switch, TagInput, TextInput, useDebounced, useLoad } from "./fields";
import { Invalid, StepFrame, useFrame } from "./frame";

const TOKENS = "https://id.atlassian.com/manage-profile/security/api-tokens";
// "you can skip this" belongs to the onboarding only: in settings there is nothing to skip
const lead = (onboarding: boolean) => (onboarding ? `${t("tickets.lead")} ${t("tickets.later")}` : t("tickets.lead"));

/** Jira, the one optional step: connect with an API token, then say which tickets are the user's and see how many that
 *  is before saving. */
export function TicketsStep({ state }: { state: SetupState }) {
  const j = state.jira;
  const [replacing, setReplacing] = useState(false);
  // the project list only comes with the answer to "connect": it is known for the rest of this visit, not after
  const [available, setAvailable] = useState<JiraProject[] | null>(null);
  return !j.connected || replacing ? (
    <Connect
      state={state}
      onDone={(p) => {
        setAvailable(p);
        setReplacing(false);
      }}
      onKeep={j.connected ? () => setReplacing(false) : undefined}
    />
  ) : (
    <Filter state={state} available={available} onReplace={() => setReplacing(true)} />
  );
}

function Connect({ state, onDone, onKeep }: { state: SetupState; onDone: (projects: JiraProject[]) => void; onKeep?: () => void }) {
  const f = useFrame();
  const [base, setBase] = useState(state.jira.base);
  const [email, setEmail] = useState(state.jira.email);
  const [token, setToken] = useState("");
  const [bad, setBad] = useState<{ field: "base" | "email" | "token"; key: Key } | null>(null);
  const say = (x: "base" | "email" | "token") => (bad?.field === x ? t(bad.key) : null);
  const edit = (set: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    set(e.target.value);
    setBad(null);
  };

  const connect = async () => {
    const b = base.trim().replace(/\/+$/, ""), m = email.trim(), k = token.trim();
    const wrong = !/^https?:\/\/[^\s/]+\.[^\s/]+/.test(b) ? ({ field: "base", key: "tickets.err.base" } as const) : !/^\S+@\S+\.\S+$/.test(m) ? ({ field: "email", key: "tickets.err.email" } as const) : !k ? ({ field: "token", key: "tickets.err.token" } as const) : null;
    if (wrong) {
      setBad(wrong);
      throw new Invalid();
    }
    const r = await api.jiraConnect(b, m, k); // a refusal shows by the Connect button, with all three fields as typed
    await refreshSetup();
    onDone(r.projects);
    return "stay" as const;
  };

  return (
    <StepFrame title={t("tickets.title")} lead={lead(f.mode === "onboarding")} save={connect} submit={t("common.connect")} skip={f.mode === "onboarding"}>
      <div className="space-y-4">
        <Field label={t("tickets.base")} error={say("base")}>
          {(p) => <TextInput {...p} type="url" inputMode="url" value={base} placeholder={t("tickets.base.placeholder")} onChange={edit(setBase)} />}
        </Field>
        <Field label={t("tickets.email")} error={say("email")}>
          {(p) => <TextInput {...p} type="email" inputMode="email" autoComplete="email" value={email} placeholder={t("tickets.email.placeholder")} onChange={edit(setEmail)} />}
        </Field>
        <Field
          label={t("tickets.token")}
          error={say("token")}
          hint={
            <a href={TOKENS} target="_blank" rel="noreferrer" className={BTN.linkSmall}>
              {t("tickets.tokenLink")} <ArrowUpRight size={11} />
            </a>
          }
        >
          {(p) => <SecretInput {...p} value={token} onChange={edit(setToken)} />}
        </Field>
      </div>
      {onKeep ? (
        <button type="button" onClick={onKeep} className={BTN.link}>
          {t("tickets.keepAccount")}
        </button>
      ) : null}
    </StepFrame>
  );
}

function Filter({ state, available, onReplace }: { state: SetupState; available: JiraProject[] | null; onReplace: () => void }) {
  const f = useFrame();
  const j = state.jira;
  const onboarding = f.mode === "onboarding";
  const [enabled, setEnabled] = useState(onboarding ? true : j.enabled);
  const [projects, setProjects] = useState(j.projects);
  // nothing saved yet: both switches start on, the tickets most people mean by "mine"
  const fresh = !j.enabled && !j.projects.length;
  const [assigned, setAssigned] = useState(fresh ? true : j.assigned);
  const [mentions, setMentions] = useState(fresh ? true : j.mentions);
  const [jql, setJql] = useState(j.jql);
  const [advanced, setAdvanced] = useState(!!j.jql);

  const filter = { projects, assigned, mentions, jql: jql.trim() };
  const asked = useDebounced(JSON.stringify(filter), 500);
  const preview = useLoad<JiraPreview>(enabled && projects.length ? `preview:${asked}` : null, () => api.jiraPreview(JSON.parse(asked)));
  const stale = asked !== JSON.stringify(filter) || preview.busy;

  const body = { enabled, ...filter };
  const toggle = (key: string) => setProjects(projects.includes(key) ? projects.filter((k) => k !== key) : [...projects, key]);
  return (
    <StepFrame
      title={t("tickets.title")}
      lead={lead(f.mode === "onboarding")}
      save={async () => {
        await api.jira(body);
        await refreshSetup();
        await loadSite(); // ticket links are built from the Jira site just saved
      }}
      ready={!enabled || projects.length > 0}
      blocked={t("tickets.needProject")}
      dirty={JSON.stringify(body) !== JSON.stringify({ enabled: j.enabled, projects: j.projects, assigned: j.assigned, mentions: j.mentions, jql: j.jql })}
      skip={onboarding}
    >
      <Connected
        action={
          <button type="button" onClick={onReplace} className={BTN.quiet}>
            {t("tickets.replace")}
          </button>
        }
      >
        {t("tickets.connected", { base: j.base.replace(/^https?:\/\//, ""), account: j.account || j.email })}
      </Connected>

      {!onboarding ? <Switch checked={enabled} onChange={setEnabled} label={t("tickets.enabled")} /> : null}

      {/* switched off: the filter stays visible but out of reach, so turning it back on loses nothing */}
      <div className={cx("space-y-7 transition-opacity", !enabled && "opacity-45")} inert={!enabled}>
        <Part title={t("tickets.projects.title")} lead={t("tickets.projects.lead")}>
          {available?.length ? (
            <div className="flex flex-wrap gap-1.5">
              {/* a saved project the new list does not have still shows, so it can be turned off */}
              {[...available, ...projects.filter((k) => !available.some((a) => a.key === k)).map((k) => ({ key: k, name: "" }))].map((p) => {
                const on = projects.includes(p.key);
                return (
                  <button
                    key={p.key}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggle(p.key)}
                    className={cx(
                      "inline-flex h-7 max-w-full items-center gap-1.5 rounded-full px-2.5 text-[13px] transition",
                      on ? "bg-brand-soft text-brand-ink shadow-[0_0_0_1px_color-mix(in_oklab,var(--brand-ink)_45%,transparent)]" : "text-fg-2 shadow-[0_0_0_0.5px_var(--line-strong)] hover:bg-hover hover:text-fg",
                    )}
                  >
                    {on ? <Check size={12} strokeWidth={2.6} className="shrink-0" /> : null}
                    <span className="font-mono text-[12px]">{p.key}</span>
                    {p.name ? <span className="truncate">{p.name}</span> : null}
                  </button>
                );
              })}
            </div>
          ) : (
            <TagInput upper value={projects} onChange={setProjects} placeholder={t("tickets.projects.addKey")} label={t("tickets.projects.title")} />
          )}
        </Part>

        <div className="space-y-3.5">
          <Switch checked={assigned} onChange={setAssigned} label={t("tickets.assigned")} />
          <Switch checked={mentions} onChange={setMentions} label={t("tickets.mentions")} />
        </div>

        <div>
          <button type="button" aria-expanded={advanced} onClick={() => setAdvanced((a) => !a)} className="inline-flex items-center gap-1 rounded-sm text-[13px] text-fg-2 hover:text-fg">
            <ChevronDown size={14} className={cx("transition", !advanced && "-rotate-90")} /> {t("tickets.jql")}
          </button>
          {advanced ? (
            <div className="fade-in mt-2">
              <TextInput mono value={jql} aria-label={t("tickets.jql")} aria-invalid={preview.failed && !!jql.trim() ? true : undefined} placeholder="labels = backend AND priority >= High" onChange={(e) => setJql(e.target.value)} />
              <p className="mt-1.5 text-[12.5px] text-fg-3">{t("tickets.jql.hint")}</p>
            </div>
          ) : null}
        </div>

        {projects.length ? (
          <Part title={t("tickets.preview")}>
            <div className="rounded-xl px-3 py-2.5 shadow-[0_0_0_0.5px_var(--line-strong)]" aria-live="polite">
              {preview.failed && !stale ? (
                // a filter Jira refused (most often the extra JQL): its own words, where the count would be
                <ErrorText className="mt-0">{preview.error}</ErrorText>
              ) : !preview.data ? (
                <p className="flex items-center gap-2 text-[13px]">
                  <Spinner size={12} /> <span className="shimmer">{t("tickets.preview.loading")}</span>
                </p>
              ) : (
                <div className={cx("transition-opacity", stale && "opacity-50")}>
                  <p className="flex items-center gap-2 text-[14px] font-medium text-fg">
                    {preview.data.count ? tn("tickets.preview.count", preview.data.count) : t("tickets.preview.none")}
                    {stale ? <Spinner size={12} /> : null}
                  </p>
                  {preview.data.sample.length ? (
                    <ul className="mt-2 space-y-1.5">
                      {preview.data.sample.slice(0, 3).map((x) => (
                        <li key={x.key} className="flex items-center gap-2 text-[13px]">
                          <span className="shrink-0 font-mono text-[12px] text-fg-3">{x.key}</span>
                          <span className="min-w-0 flex-1 truncate text-fg-2">{x.summary}</span>
                          <Tag>{x.status}</Tag>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              )}
            </div>
          </Part>
        ) : null}
      </div>
    </StepFrame>
  );
}

import { ArrowUpRight, Check, FolderGit2, Lock, Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { rich, t, tn } from "../../i18n";
import { ago, cx, loadSite } from "../../lib";
import { api, loadSetup, refreshSetup, type GithubRepo, type RepoChoice, type SetupState } from "../../setup";
import { BTN, Connected, Field, LoadFailed, Notice, Part, SearchInput, Select, TextInput, useDebounced, useLoad } from "./fields";
import { Invalid, StepFrame } from "./frame";

/** GitHub: which account or organization, and which repositories Clea works on. Clea uses the GitHub CLI already on
 *  this Mac, so while that is missing or signed out the step says what to do and waits. */
export function CodeStep({ state }: { state: SetupState }) {
  const g = state.github;
  const ready = g.cli.installed && g.connected;

  // signing in happens in the terminal: notice it by itself instead of asking for a click
  useEffect(() => {
    if (ready) return;
    const h = setInterval(() => !document.hidden && loadSetup(), 3000);
    return () => clearInterval(h);
  }, [ready]);

  if (ready) return <Repos state={state} />;
  return (
    <StepFrame
      title={t("code.title")}
      lead={t("code.lead")}
      submit={t("common.checkAgain")}
      save={async () => {
        await refreshSetup();
        return "stay";
      }}
    >
      {!g.cli.installed ? (
        <Notice tone="neutral" title={<span className="text-fg">{t("code.gh.missing.title")}</span>}>
          <p>{t("code.gh.missing.body")}</p>
          <a href="https://cli.github.com" target="_blank" rel="noreferrer" className={cx(BTN.secondary, "mt-3")}>
            {t("code.gh.install")} <ArrowUpRight size={13} className="opacity-60" />
          </a>
        </Notice>
      ) : (
        <Notice tone="neutral" title={<span className="text-fg">{t("code.gh.login.title")}</span>}>
          <p>{rich("code.gh.login.body", { cmd: <code data-selectable className="rounded-md bg-active px-1.5 py-0.5 font-mono text-[12px] text-fg">gh auth login</code> })}</p>
        </Notice>
      )}
    </StepFrame>
  );
}

function Repos({ state }: { state: SetupState }) {
  const g = state.github;
  const [org, setOrg] = useState(g.org || g.login);
  const [repos, setRepos] = useState<RepoChoice[]>(g.repos);
  const [noBranch, setNoBranch] = useState(false);
  const [q, setQ] = useState("");
  const text = useDebounced(q.trim(), 250);
  const orgs = useLoad("orgs", api.githubOrgs);
  const found = useLoad<GithubRepo[]>(`repos:${org}:${text}`, () => api.githubRepos(org, text));

  // the repositories belong to one account or organization: moving to another starts the selection over
  const pickOrg = (next: string) => {
    if (next === org) return;
    setOrg(next);
    setRepos(next === g.org ? g.repos : []);
    setQ("");
  };
  const toggle = (r: GithubRepo) =>
    setRepos(repos.some((x) => x.full_name === r.full_name) ? repos.filter((x) => x.full_name !== r.full_name) : [...repos, { name: r.name, full_name: r.full_name, path: r.path, default: r.default }]);
  const patch = (full: string, change: Partial<RepoChoice>) => setRepos(repos.map((x) => (x.full_name === full ? { ...x, ...change } : x)));

  // the user's own account first, then their organizations; the saved one stays listed even if GitHub no longer returns it
  const me = orgs.data?.login ?? "";
  const options = orgs.data ? [{ login: me, name: t("code.org.you", { login: me }) }, ...orgs.data.orgs.filter((o) => o.login !== me)] : [];
  if (org && !options.some((o) => o.login === org)) options.push({ login: org, name: org });
  useEffect(() => {
    if (!org && me) setOrg(me);
  }, [org, me]);
  return (
    <StepFrame
      title={t("code.title")}
      lead={t("code.lead")}
      save={async () => {
        if (repos.some((r) => !r.default.trim())) {
          setNoBranch(true);
          throw new Invalid();
        }
        await api.github(org, repos.map((r) => ({ ...r, path: r.path.trim(), default: r.default.trim() })));
        await refreshSetup();
        await loadSite(); // PR links are built from the organization just saved
      }}
      ready={repos.length > 0}
      blocked={t("code.needRepo")}
      dirty={org !== g.org || JSON.stringify(repos) !== JSON.stringify(g.repos)}
    >
      <Connected>{t("code.gh.connected", { login: g.login })}</Connected>

      <Field label={t("code.org")} error={orgs.failed ? t("code.orgs.failed") : null}>
        {(p) => (
          <Select {...p} value={org} onChange={(e) => pickOrg(e.target.value)} className="w-full sm:w-[320px]">
            {options.map((o) => (
              <option key={o.login} value={o.login}>
                {o.name && o.name !== o.login ? o.name : o.login}
              </option>
            ))}
          </Select>
        )}
      </Field>

      <Part title={t("code.repos.title")} right={repos.length ? <span className="text-[12.5px] text-fg-3">{tn("code.repos.chosen", repos.length)}</span> : null}>
        <div className="space-y-2.5">
          {repos.length ? (
            <ul className="space-y-2">
              {repos.map((r) => (
                <li key={r.full_name} className="rounded-xl bg-[var(--field)] px-3 pt-2 pb-3 shadow-[0_0_0_0.5px_var(--line-strong)]">
                  <div className="flex items-center gap-2">
                    <FolderGit2 size={14} className="shrink-0 text-fg-3" />
                    <span className="min-w-0 flex-1 truncate font-mono text-[13px] text-fg">{r.name}</span>
                    <button type="button" aria-label={t("code.repo.remove", { name: r.name })} onClick={() => setRepos(repos.filter((x) => x.full_name !== r.full_name))} className="-mr-1.5 grid size-7 shrink-0 place-items-center rounded-lg text-fg-3 hover:bg-hover hover:text-fg">
                      <X size={14} />
                    </button>
                  </div>
                  <div className="mt-1.5 grid gap-x-3 gap-y-2.5 sm:grid-cols-[minmax(0,1fr)_150px]">
                    <Field label={<span className="text-[12px] font-normal text-fg-2">{t("code.repo.path")}</span>} hint={r.path.trim() ? undefined : t("code.repo.cloneHint")}>
                      {(p) => <TextInput {...p} mono compact value={r.path} placeholder={t("code.repo.clone")} onChange={(e) => patch(r.full_name, { path: e.target.value })} />}
                    </Field>
                    <Field label={<span className="text-[12px] font-normal text-fg-2">{t("code.repo.branch")}</span>} error={noBranch && !r.default.trim() ? t("code.repo.branchRequired") : null}>
                      {(p) => <TextInput {...p} mono compact value={r.default} onChange={(e) => patch(r.full_name, { default: e.target.value })} />}
                    </Field>
                  </div>
                </li>
              ))}
            </ul>
          ) : null}

          <SearchInput value={q} onChange={setQ} placeholder={t("code.repos.search")} busy={found.busy || q.trim() !== text} />
          <div className="max-h-[232px] overflow-y-auto rounded-xl shadow-[0_0_0_0.5px_var(--line-strong)]">
            {found.failed ? (
              <LoadFailed retry={found.retry}>{t("code.repos.failed")}</LoadFailed>
            ) : !found.data ? (
              <p className="shimmer px-3 py-3 text-[13px]">{t("common.loading")}</p>
            ) : !found.data.length ? (
              <p className="px-3 py-3 text-[13px] text-fg-3">{t("code.repos.none")}</p>
            ) : (
              <ul>
                {found.data.map((r, i) => {
                  const on = repos.some((x) => x.full_name === r.full_name);
                  return (
                    <li key={r.full_name} className={cx(i > 0 && "border-t-[0.5px] border-line")}>
                      <button type="button" aria-pressed={on} onClick={() => toggle(r)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition hover:bg-hover">
                        <span className={cx("grid size-4 shrink-0 place-items-center rounded-[5px] transition", on ? "bg-brand text-white" : "shadow-[inset_0_0_0_1px_var(--line-strong)]")}>
                          {on ? <Check size={11} strokeWidth={3.2} /> : null}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-1.5">
                            <span className="truncate font-mono text-[13px] text-fg">{r.name}</span>
                            {r.private ? <Lock size={11} className="shrink-0 text-fg-3" aria-label={t("code.repo.private")} /> : null}
                          </span>
                          <span className="block truncate text-[12px] text-fg-3">
                            {[r.language, r.pushed_at ? t("code.repo.pushed", { ago: ago(Date.parse(r.pushed_at) / 1000) }) : "", r.path].filter(Boolean).join(" · ")}
                          </span>
                        </span>
                        {!on ? <Plus size={15} className="shrink-0 text-fg-3" /> : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      </Part>
    </StepFrame>
  );
}

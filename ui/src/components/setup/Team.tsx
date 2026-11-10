import { Plus, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { t } from "../../i18n";
import { cx } from "../../lib";
import { api, refreshSetup, type SetupState, type SlackUser, type TeamMember } from "../../setup";
import { ErrorText, LoadFailed, Part, SearchInput, TagInput, TextArea, TextInput, useDebounced, useLoad } from "./fields";
import { Invalid, StepFrame } from "./frame";

/** The people the user works with and what the team owns. The teammates start from a suggestion (the team channel's
 *  members matched to GitHub logins) that the user corrects; the area is free text Clea reads to decide what is theirs. */
export function TeamStep({ state }: { state: SetupState }) {
  const s = state.team;
  const [members, setMembers] = useState<TeamMember[]>(s.members);
  const [area, setArea] = useState(s.area);
  const [keywords, setKeywords] = useState(s.keywords);
  const [checked, setChecked] = useState(false);

  // nothing saved yet: ask once who is in the team channel. A failed suggestion is not an error, the list just starts empty.
  const fresh = !s.members.length && !state.steps.find((x) => x.id === "team")?.done;
  const [suggesting, setSuggesting] = useState(fresh);
  const touched = useRef(false);
  useEffect(() => {
    if (!fresh) return;
    let on = true;
    api.teamSuggest()
      .then((r) => on && !touched.current && setMembers(r.members))
      .catch(() => {})
      .finally(() => on && setSuggesting(false));
    return () => {
      on = false;
    };
  }, [fresh]);

  const change = (next: TeamMember[]) => {
    touched.current = true;
    setMembers(next);
  };
  const patch = (i: number, c: Partial<TeamMember>) => change(members.map((m, k) => (k === i ? { ...m, ...c } : m)));

  const clean = { members: members.map((m) => ({ slack_id: m.slack_id, name: m.name.trim(), github: m.github.trim().replace(/^@/, "") })), area: area.trim(), keywords };
  const noName = clean.members.some((m) => !m.name);
  return (
    <StepFrame
      title={t("team.title")}
      lead={t("team.lead")}
      save={async () => {
        if (!clean.area || noName) {
          setChecked(true);
          throw new Invalid();
        }
        await api.team(clean);
        await refreshSetup();
      }}
      dirty={JSON.stringify(clean) !== JSON.stringify({ members: s.members, area: s.area, keywords: s.keywords })}
    >
      <Part title={t("team.members.title")} lead={t("team.members.lead")}>
        <div className="space-y-2.5">
          {suggesting ? (
            <p className="shimmer py-1 text-[13px]">{t("team.members.suggesting")}</p>
          ) : members.length ? (
            <div>
              <div className="mb-1 grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_28px] gap-2 px-0.5 text-[12px] text-fg-3">
                <span>{t("team.members.name")}</span>
                <span>{t("team.members.github")}</span>
              </div>
              <ul className="space-y-1.5">
                {members.map((m, i) => (
                  <li key={m.slack_id || i} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_28px] items-center gap-2">
                    <TextInput
                      compact
                      value={m.name}
                      aria-label={t("team.members.name")}
                      aria-invalid={checked && !m.name.trim() ? true : undefined}
                      onChange={(e) => patch(i, { name: e.target.value })}
                    />
                    <label className="field flex h-8 min-w-0 items-center rounded-lg pl-2.5 text-fg-3">
                      <span className="text-[13px]">@</span>
                      <input
                        type="text"
                        value={m.github}
                        aria-label={t("team.members.github")}
                        autoComplete="off"
                        spellCheck={false}
                        onChange={(e) => patch(i, { github: e.target.value.replace(/^@/, "") })}
                        className="h-full w-full min-w-0 bg-transparent pr-2.5 pl-0.5 font-mono text-[13px] text-fg outline-none"
                      />
                    </label>
                    <button type="button" aria-label={t("team.members.remove", { name: m.name })} onClick={() => change(members.filter((_, k) => k !== i))} className="grid size-7 place-items-center rounded-lg text-fg-3 hover:bg-hover hover:text-fg">
                      <X size={14} />
                    </button>
                  </li>
                ))}
              </ul>
              {checked && noName ? <p role="alert" className="mt-1.5 text-[12.5px] text-red">{t("team.members.nameRequired")}</p> : null}
            </div>
          ) : (
            <p className="text-[13px] text-fg-3">{t("team.members.empty")}</p>
          )}
          <AddFromSlack have={members} onAdd={(u) => change([...members, { slack_id: u.id, name: u.real_name || u.name, github: "" }])} />
        </div>
      </Part>

      <Part title={t("team.area.title")} lead={t("team.area.label")}>
        <TextArea
          rows={3}
          value={area}
          aria-label={t("team.area.title")}
          aria-invalid={checked && !clean.area ? true : undefined}
          placeholder={t("team.area.placeholder")}
          onChange={(e) => setArea(e.target.value)}
        />
        {checked && !clean.area ? <ErrorText>{t("team.area.required")}</ErrorText> : null}
      </Part>

      <Part title={t("team.keywords.title")} lead={t("team.keywords.lead")}>
        <TagInput value={keywords} onChange={setKeywords} placeholder={t("slack.keywords.placeholder")} label={t("team.keywords.title")} />
      </Part>
    </StepFrame>
  );
}

/** Finds a person in Slack by name. The list only opens once something is typed: the workspace can be thousands of
 *  people, and a wall of names is not a suggestion. */
function AddFromSlack({ have, onAdd }: { have: TeamMember[]; onAdd: (u: SlackUser) => void }) {
  const [q, setQ] = useState("");
  const text = useDebounced(q.trim(), 250);
  const found = useLoad<SlackUser[]>(text ? `users:${text}` : null, () => api.slackUsers(text));
  const rows = (found.data ?? []).filter((u) => !have.some((m) => m.slack_id === u.id));
  return (
    <div>
      <SearchInput value={q} onChange={setQ} placeholder={t("team.members.search")} busy={!!q.trim() && (found.busy || q.trim() !== text)} />
      {text && q.trim() ? (
        <div className="mt-1.5 max-h-[188px] overflow-y-auto rounded-xl shadow-[0_0_0_0.5px_var(--line-strong)]">
          {found.failed ? (
            <LoadFailed retry={found.retry}>{t("team.members.failed")}</LoadFailed>
          ) : !found.data ? (
            <p className="shimmer px-3 py-2.5 text-[13px]">{t("common.loading")}</p>
          ) : !rows.length ? (
            <p className="px-3 py-2.5 text-[13px] text-fg-3">{t("team.members.none")}</p>
          ) : (
            <ul>
              {rows.map((u, i) => (
                <li key={u.id} className={cx(i > 0 && "border-t-[0.5px] border-line")}>
                  <button
                    type="button"
                    onClick={() => {
                      onAdd(u);
                      setQ("");
                    }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-hover"
                  >
                    <span className="min-w-0 flex-1 truncate text-[14px] text-fg">
                      {u.real_name || u.name}
                      <span className="ml-2 text-[12.5px] text-fg-3">{[u.name && `@${u.name}`, u.title].filter(Boolean).join(" · ")}</span>
                    </span>
                    <Plus size={15} className="shrink-0 text-fg-3" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}

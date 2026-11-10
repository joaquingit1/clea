import { ArrowUpRight, Check } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { locale, t, type Key } from "../../i18n";
import { cx } from "../../lib";
import { api, errorText, loadSetup, refreshSetup, type LoginState, type ProviderId, type ProviderState, type SetupState } from "../../setup";
import { Spinner, Tag } from "../ui";
import { BTN, ErrorText, SecretInput } from "./fields";
import { StepFrame, useFrame } from "./frame";

const USAGE: Record<ProviderId, Key> = {
  "claude-subscription": "model.usage.claudePlan",
  "claude-api": "model.usage.api",
  "openai-chatgpt": "model.usage.chatgptPlan",
  "codex-login": "model.usage.chatgptPlan",
  "openai-api": "model.usage.api",
};
// where each vendor hands out keys
const KEY_URL: Partial<Record<ProviderId, string>> = {
  "claude-api": "https://console.anthropic.com/settings/keys",
  "openai-api": "https://platform.openai.com/api-keys",
};
const KEY_SHAPE: Partial<Record<ProviderId, string>> = { "claude-api": "sk-ant-…", "openai-api": "sk-…" };
const CHATGPT_USAGE = "https://chatgpt.com/settings/usage";
const WELCOMED = "chatgpt-plan-welcomed";

/** "starting": the request to open the sign-in is on its way, and the worker may still be reporting the attempt before
 *  it, so nothing is polled yet. "waiting": it is open, and polled until it ends. */
interface Login {
  id: ProviderId;
  state: "starting" | "waiting" | "error";
  url: string | null;
  error: string | null;
}

// A sign-in the user cancelled here. The API has no way to call one off, so the worker may still report it as waiting:
// this keeps a cancelled sign-in from coming back when the step is opened again.
let dismissed: string | null = null;

/** Which model Clea runs on. Every way in is the vendor's own: a sign-in on their page, their CLI already signed in on
 *  this Mac, or a key kept in the Keychain. Exactly one is in use. */
export function ModelStep({ state }: { state: SetupState }) {
  const f = useFrame();
  const [login, setLogin] = useState<Login | null>(null);
  const [welcome, setWelcome] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true; // set on every mount: React's strict mode mounts, unmounts and mounts again
    return () => {
      alive.current = false;
    };
  }, []);

  // a sign-in finished in the terminal or the browser shows up by itself
  useEffect(() => {
    const h = setInterval(() => !document.hidden && loadSetup(), 3000);
    return () => clearInterval(h);
  }, []);

  // came back to the step while a sign-in was still open: pick it up where it was
  useEffect(() => {
    api.loginState()
      .then((l) => {
        if (alive.current && l.state === "waiting" && l.id && l.id !== dismissed) setLogin({ id: l.id as ProviderId, state: "waiting", url: l.url, error: null });
      })
      .catch(() => {});
  }, []);

  const finish = async (id: ProviderId) => {
    await api.provider(id); // signing in is choosing it
    await refreshSetup();
    if (!alive.current) return;
    setLogin(null);
    if (id === "openai-chatgpt") {
      // OpenAI's guidelines: confirm plan use once, the first time, and never again
      let seen = false;
      try {
        seen = localStorage.getItem(WELCOMED) === "1";
        localStorage.setItem(WELCOMED, "1");
      } catch {
        /* storage blocked: it shows again next time, which is the safe side */
      }
      if (!seen) setWelcome(true);
    }
  };

  const settle = async (id: ProviderId, l: LoginState) => {
    if (l.state === "done") await finish(id);
    else if (l.state === "error") setLogin({ id, state: "error", url: null, error: l.error || t("model.loginFailed") });
    else if (l.state === "waiting") setLogin({ id, state: "waiting", url: l.url, error: null });
    else setLogin(null); // idle: the worker dropped it (a restart); the button is there to start again
  };

  // while a sign-in is open, ask every 1.5 s how it is going
  const waiting = login?.state === "waiting" ? login.id : null;
  useEffect(() => {
    if (!waiting) return;
    let on = true;
    const h = setInterval(async () => {
      try {
        const l = await api.loginState();
        if (on && l.id === waiting && l.state !== "waiting") await settle(waiting, l);
      } catch (e) {
        if (on) setLogin({ id: waiting, state: "error", url: null, error: errorText(e) });
      }
    }, 1500);
    return () => {
      on = false;
      clearInterval(h);
    };
  }, [waiting]);

  const attempt = useRef(0); // a cancel while the sign-in is still opening must not be undone by its late answer
  const start = async (id: ProviderId) => {
    const my = ++attempt.current;
    dismissed = null;
    setLogin({ id, state: "starting", url: null, error: null });
    try {
      const l = await api.login(id);
      if (my === attempt.current) await settle(id, l);
      else dismissed = id;
    } catch (e) {
      if (my === attempt.current) setLogin({ id, state: "error", url: null, error: errorText(e) });
    }
  };
  const cancel = () => {
    attempt.current++;
    dismissed = login?.id ?? null;
    setLogin(null);
    api.loginCancel().catch(() => {}); // the worker stops waiting too (an older one has no such route: the note above still covers it)
  };

  const chosen = state.providers.find((p) => p.selected && p.ready);
  const vendors = [...new Set(state.providers.map((p) => p.vendor))];
  return (
    <StepFrame
      title={t("model.title")}
      lead={f.mode === "onboarding" ? `${t("model.lead")} ${t("model.later")}` : t("model.lead")}
      // onboarding: Continue confirms the one in use; settings has no button, every card acts at once
      save={
        f.mode === "onboarding"
          ? async () => {
              if (chosen) await api.provider(chosen.id);
              await refreshSetup();
            }
          : undefined
      }
      ready={!!chosen}
      blocked={t("model.needOne")}
      focus={false}
    >
      {vendors.map((v) => (
        <section key={v}>
          <h2 className="mb-2 text-[12px] font-medium tracking-wide text-fg-3">{v}</h2>
          <div className="space-y-2">
            {state.providers
              .filter((p) => p.vendor === v)
              .map((p) => (
                <ProviderCard key={p.id} p={p} login={login?.id === p.id ? login : null} busyElsewhere={!!login && login.state !== "error" && login.id !== p.id} onLogin={() => start(p.id)} onCancel={cancel} />
              ))}
          </div>
        </section>
      ))}
      {welcome ? <ChatGptWelcome onClose={() => setWelcome(false)} /> : null}
    </StepFrame>
  );
}

function ProviderCard({ p, login, busyElsewhere, onLogin, onCancel }: { p: ProviderState; login: Login | null; busyElsewhere: boolean; onLogin: () => void; onCancel: () => void }) {
  const f = useFrame();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<"key" | "use" | "check" | "out" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const on = p.selected && p.ready;
  const cli = t(`cli.${p.cli.name}`);

  const run = async (what: NonNullable<typeof busy>, fn: () => Promise<unknown>) => {
    setBusy(what);
    setError(null);
    try {
      await fn();
      await refreshSetup();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setBusy(null);
    }
  };
  const saveKey = async () => {
    if (!key.trim()) return setError(t("model.key.empty"));
    if (await run("key", () => api.provider(p.id, key.trim()))) setKey(""); // a refused key stays in the box to be fixed
  };

  const waiting = login?.state === "waiting" || login?.state === "starting";
  // a stored key or the ChatGPT sign-in can be forgotten from settings; a CLI's own login is never Clea's to touch
  const canDisconnect = f.mode === "settings" && (p.auth === "api_key" || p.auth === "siwc") && (p.ready || !!p.account);
  return (
    <div className="choice rounded-2xl px-4 py-3.5" data-on={on || undefined}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h3 className="text-[14px] font-semibold text-fg">{t(`provider.${p.id}.name`)}</h3>
        <Tag>{t(USAGE[p.id])}</Tag>
        <span className="ml-auto">
          {on ? (
            <Tag tone="accent">
              <Check size={11} strokeWidth={3} /> {t("model.inUse")}
            </Tag>
          ) : p.ready ? (
            <span className="inline-flex items-center gap-1 text-[12.5px] text-green">
              <Check size={13} strokeWidth={2.6} /> {t("model.ready")}
            </span>
          ) : null}
        </span>
      </div>
      <p className="mt-1.5 text-[13px] leading-snug text-fg-2">{t(`provider.${p.id}.what`)}</p>

      {p.ready && (p.account || p.plan) ? (
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-fg">
          {p.account ? <span className="[overflow-wrap:anywhere]">{p.account}</span> : null}
          {p.plan ? <Tag>{p.plan}</Tag> : null}
          {p.expires_at ? <span className="text-[12.5px] text-fg-3">{t("model.expires", { date: day(p.expires_at) })}</span> : null}
        </p>
      ) : null}
      {/* the worker's own sentence about where this stands, when it has one and the card is not already saying it */}
      {p.detail && !waiting ? <p className={cx("mt-2 text-[13px] leading-snug", p.ready ? "text-fg-3" : "text-fg-2")}>{p.detail}</p> : null}
      {!p.detail && p.needs === "install" ? <p className="mt-2 text-[13px] leading-snug text-fg-2">{t("model.notInstalled", { cli })}</p> : null}

      {p.id === "openai-chatgpt" && p.ready ? (
        // OpenAI's guidelines: say when the plan is in use, with the way to manage it beside it
        <p className="mt-2 flex flex-wrap items-center gap-x-2 text-[13px]">
          {on ? <span className="font-medium text-fg">{t("model.chatgpt.using")}</span> : null}
          <a href={CHATGPT_USAGE} target="_blank" rel="noreferrer" className={BTN.link}>
            {t("model.chatgpt.manage")} <ArrowUpRight size={12} />
          </a>
        </p>
      ) : null}

      {p.needs === "key" ? (
        <div className="mt-3">
          <div className="flex flex-wrap gap-2">
            <div className="min-w-[12rem] flex-1">
              <SecretInput
                value={key}
                aria-label={`${t(`provider.${p.id}.name`)}: ${t("model.key")}`}
                aria-invalid={error ? true : undefined}
                placeholder={KEY_SHAPE[p.id]}
                onChange={(e) => {
                  setKey(e.target.value);
                  setError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  e.preventDefault(); // Enter here checks this key, it does not continue the step
                  saveKey();
                }}
              />
            </div>
            <button type="button" onClick={saveKey} disabled={busy !== null} className={BTN.secondary}>
              {busy === "key" ? <Spinner size={13} /> : null}
              {busy === "key" ? t("model.key.checking") : t(`provider.${p.id}.action`)}
            </button>
          </div>
          {error ? (
            <ErrorText>{error}</ErrorText>
          ) : (
            <p className="mt-1.5 text-[12.5px] leading-snug text-fg-3">
              {t("model.key.kept")}{" "}
              {KEY_URL[p.id] ? (
                <a href={KEY_URL[p.id]} target="_blank" rel="noreferrer" className={BTN.linkSmall}>
                  {t("model.key.get")} <ArrowUpRight size={11} />
                </a>
              ) : null}
            </p>
          )}
        </div>
      ) : null}

      {p.needs === "install" ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <a href={p.cli.install_url} target="_blank" rel="noreferrer" className={BTN.secondary}>
            {t("model.install", { cli })} <ArrowUpRight size={13} className="opacity-60" />
          </a>
          <button type="button" onClick={() => run("check", async () => {})} disabled={busy !== null} className={BTN.ghost}>
            {busy === "check" ? <Spinner size={13} /> : null}
            {t("common.checkAgain")}
          </button>
        </div>
      ) : null}

      {p.needs === "login" ? (
        waiting ? (
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px]">
            <span className="inline-flex items-center gap-2">
              <Spinner size={13} />
              <span className="shimmer">{t("model.waiting")}</span>
            </span>
            {login?.url ? (
              <a href={login.url} target="_blank" rel="noreferrer" className={BTN.link}>
                {t("model.openAgain")} <ArrowUpRight size={12} />
              </a>
            ) : null}
            <button type="button" onClick={onCancel} className={cx(BTN.ghostSmall, "ml-auto")}>
              {t("common.cancel")}
            </button>
          </div>
        ) : (
          <div className="mt-3">
            <button type="button" onClick={onLogin} disabled={busyElsewhere} className={p.id === "openai-chatgpt" ? BTN.ink : BTN.secondary}>
              {t(`provider.${p.id}.action`)}
            </button>
            {login?.state === "error" ? <ErrorText>{login.error}</ErrorText> : null}
          </div>
        )
      ) : null}

      {(p.ready && !p.selected) || canDisconnect ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {p.ready && !p.selected ? (
            <button type="button" onClick={() => run("use", () => api.provider(p.id))} disabled={busy !== null} className={BTN.secondary}>
              {busy === "use" ? <Spinner size={13} /> : null}
              {t("model.useThis")}
            </button>
          ) : null}
          {canDisconnect ? (
            <button type="button" onClick={() => run("out", () => api.logout(p.id))} disabled={busy !== null} className={BTN.small}>
              {busy === "out" ? <Spinner size={13} /> : null}
              {t("model.disconnect")}
            </button>
          ) : null}
        </div>
      ) : null}
      {error && p.needs !== "key" ? <ErrorText>{error}</ErrorText> : null}
    </div>
  );
}

function day(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString(locale(), { day: "numeric", month: "short" });
}

/** OpenAI's first-sign-in confirmation: shown once, says where the usage goes and where to manage it, one button. */
function ChatGptWelcome({ onClose }: { onClose: () => void }) {
  const btn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    btn.current?.focus();
    const on = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  // on the body, not inside the step: the step's entrance animation would make the step the box a fixed layer fills
  return createPortal(
    <div className="fade-in fixed inset-0 z-50 grid place-items-center bg-[rgba(0,0,0,0.22)] p-5 backdrop-blur-[3px]" onMouseDown={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="chatgpt-welcome" onMouseDown={(e) => e.stopPropagation()} className="w-full max-w-[380px] rounded-3xl bg-elev p-6 text-center shadow-[var(--shadow-lg)]">
        <h2 id="chatgpt-welcome" className="text-[17px] font-semibold tracking-[-0.01em] text-fg">
          {t("model.chatgpt.welcomeTitle")}
        </h2>
        <p className="mt-2 text-[14px] leading-[1.5] text-fg-2">{t("model.chatgpt.welcomeBody")}</p>
        <a href={CHATGPT_USAGE} target="_blank" rel="noreferrer" className={cx(BTN.link, "mt-3")}>
          {t("model.chatgpt.manage")} <ArrowUpRight size={12} />
        </a>
        <button ref={btn} type="button" onClick={onClose} className={cx(BTN.ink, "mt-5 w-full")}>
          {t("model.chatgpt.gotIt")}
        </button>
      </div>
    </div>,
    document.body,
  );
}

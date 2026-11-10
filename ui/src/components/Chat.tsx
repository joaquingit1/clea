import { motion, AnimatePresence } from "motion/react";
import { Check, Copy, Zap } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { sendChat, usePoll, type ChatMessage, type Conversation } from "../api";
import { t, tn, type Key } from "../i18n";
import { clock, cx, since } from "../lib";
import { onChatGptPlan, seesProduction } from "../setup";
import { Composer, EnterHint } from "./Composer";
import { AppIcon, Mark, Md } from "./ui";

const SUGGEST: Key[] = ["chat.suggest.1", "chat.suggest.2", "chat.suggest.3", "chat.suggest.4"];

/** A conversation with the orchestrator: the same agent as the Slack DM, with the whole harness in view. */
export function ChatThread({
  conv,
  item,
  onConv,
  compact,
  emptyTitle,
}: {
  conv: string | null;
  item?: number | null;
  onConv: (conv: string) => void;
  compact?: boolean;
  emptyTitle?: string;
}) {
  const [fast, setFast] = useState(false);
  const { data, refresh } = usePoll<Conversation>(conv ? `/api/chat/${encodeURIComponent(conv)}` : null, fast ? 400 : 6000);
  const [pending, setPending] = useState<ChatMessage | null>(null);
  const [now, setNow] = useState(Date.now());
  const end = useRef<HTMLDivElement>(null);

  const streaming = !!data?.messages.some((m) => m.meta?.streaming);
  const thinking = (!!data?.thinking || !!pending) && !streaming;
  useEffect(() => setFast(thinking || streaming || !!data?.thinking), [thinking, streaming, data?.thinking]);
  useEffect(() => {
    if (!thinking) return;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [thinking]);

  const msgs = useMemo(() => {
    const m = data?.messages ?? [];
    if (pending && !m.some((x) => x.role === "user" && x.text === pending.text && x.at >= pending.at - 5)) return [...m, pending];
    return m;
  }, [data, pending]);

  useEffect(() => {
    if (pending && data?.messages.some((x) => x.role === "user" && x.text === pending.text)) setPending(null);
  }, [data, pending]);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [msgs.length, thinking]);

  const send = async (text: string) => {
    setPending({ id: -1, role: "user", text, at: Date.now() / 1000, meta: { pending: true } });
    try {
      const c = await sendChat(text, conv, item);
      if (c !== conv) onConv(c);
      refresh();
    } catch (e) {
      setPending(null);
      throw e;
    }
  };

  const empty = msgs.length === 0;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className={cx("min-h-0 flex-1 overflow-y-auto", compact ? "px-5" : "px-6")}>
        <div className={cx("mx-auto w-full", compact ? "max-w-none" : "max-w-[48rem]")}>
          {empty ? (
            <EmptyChat title={emptyTitle} compact={compact} onPick={send} item={item} />
          ) : (
            <div className={cx("flex flex-col gap-4", compact ? "py-5" : "pt-8 pb-8")}>
              <AnimatePresence initial={false}>
                {msgs.map((m) => (
                  <motion.div
                    key={m.id === -1 ? "pending" : m.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.3, ease: [0.37, 0.55, 0.86, 0.88] }}
                  >
                    <Message m={m} />
                  </motion.div>
                ))}
              </AnimatePresence>
              {thinking ? <Thinking since={data?.since ?? pending?.at ?? null} step={data?.step ?? null} now={now} /> : null}
              <div ref={end} />
            </div>
          )}
        </div>
      </div>
      <div className={cx("shrink-0 pb-4", compact ? "px-4" : "px-6")}>
        <div className={cx("mx-auto w-full", compact ? "" : "max-w-[48rem]")}>
          <Composer
            onSend={send}
            busy={thinking}
            autoFocus={!compact}
            placeholder={t(item ? "chat.askCase" : "board.ask")}
            hint={item ? t("chat.caseHint") : undefined}
          />
          {!compact ? (
            <p className="mt-2 text-center text-[12px] text-fg-3">
              {t("chat.footnote")} <EnterHint />
            </p>
          ) : null}
          {!compact && onChatGptPlan() ? (
            // OpenAI's sign-in guidelines: say where requests start that they draw from the ChatGPT plan, with the way to manage it
            <p className="mt-1 text-center text-[12px] text-fg-3">
              {t("model.chatgpt.using")} ·{" "}
              <a href="https://chatgpt.com/settings/usage" target="_blank" rel="noreferrer" className="underline decoration-line-strong underline-offset-[3px] hover:text-fg-2">
                {t("model.chatgpt.manage")}
              </a>
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function EmptyChat({ title, compact, onPick, item }: { title?: string; compact?: boolean; onPick: (t: string) => void; item?: number | null }) {
  if (compact)
    return (
      <div className="py-6 text-[13px] text-fg-3">
        {title ?? t("chat.emptyCase")}
      </div>
    );
  return (
    <div className="flex min-h-[48vh] flex-col items-center justify-end pt-10 pb-8 text-center">
      <AppIcon size={60} intro />
      <h1 className="mt-6 text-[32px] leading-[2.375rem] font-normal tracking-[-0.025em] text-fg">
        {title ?? t("chat.emptyTitle")}
      </h1>
      <p className="mt-3 max-w-[460px] text-[13px] text-fg-3">
        {t(seesProduction() ? "chat.emptyLead" : "chat.emptyLeadNoProd")}
      </p>
      {!item ? (
        <div className="mt-8 grid w-full max-w-[560px] gap-0.5 text-left">
          {SUGGEST.map((k) => (
            <button
              key={k}
              onClick={() => onPick(t(k))}
              className="flex min-h-11 items-center rounded-2xl py-2 ps-3 text-[14px] text-fg-2 transition hover:bg-hover hover:text-fg active:scale-[0.99]"
            >
              {t(k)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Message({ m }: { m: ChatMessage }) {
  if (m.role === "user")
    return (
      <div className="flex justify-end">
        <div
          className={cx(
            "max-w-[70%] rounded-[22px] bg-user px-4 py-2.5 text-[14px] leading-[1.625] break-words whitespace-pre-wrap text-fg",
            m.meta?.pending && "opacity-70",
          )}
        >
          {m.text}
        </div>
      </div>
    );
  if (m.meta?.ack)  // the instant acknowledgement: a quiet line, not an answer
    return <p className="text-[14px] text-fg-2">{m.text}</p>;
  if (m.role === "system")
    return <div className="mx-auto max-w-[520px] rounded-xl bg-amber-soft px-3 py-2 text-center text-[13px] text-amber">{m.text}</div>;
  return (
    <div className="group">
      <div className="min-w-0">
        <Md>{m.text}</Md>
        {m.meta?.streaming ? <span className="ml-0.5 inline-block h-[1.1em] w-[2px] translate-y-[3px] animate-pulse bg-fg-2" /> : null}
        {m.meta?.actions?.length ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {m.meta.actions.map((a) => (
              <span key={a} className="inline-flex items-center gap-1 rounded-full bg-purple-soft py-0.5 ps-2 pe-2.5 text-[12px] text-purple">
                <Zap size={11} /> {actionLabel(a)}
              </span>
            ))}
          </div>
        ) : null}
        <div className="mt-2 flex items-center gap-3 text-[12px] text-fg-3 opacity-0 transition group-hover:opacity-100">
          <span className="tabular-nums">{clock(m.at)}</span>
          <CopyBtn text={m.text} />
        </div>
        {m.meta?.secs ? (
          <div className="mt-3 flex items-center gap-3 text-[13px] text-fg-3">
            <span className="tabular-nums">
              {t(m.meta.fast ? "chat.answeredIn" : "chat.workedFor", { t: dur(m.meta.secs) })}
              {m.meta.steps ? ` · ${tn("chat.steps", m.meta.steps)}` : ""}
            </span>
            <span className="h-px flex-1 bg-line" />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function dur(s: number): string {
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

/** What Clea did during an answer, as the worker logs it, said the way a person would. An action it does not know
 *  shows as logged. */
const ACTIONS: [RegExp, Key][] = [
  [/^case #(\d+) created$/, "chat.action.created"],
  [/^case #(\d+) requeued.*$/, "chat.action.requeuedCase"],
  [/^#(\d+) prioritize$/, "chat.action.prioritized"],
  [/^#(\d+) requeue$/, "chat.action.requeued"],
  [/^#(\d+) drop_case$/, "chat.action.dropped"],
  [/^worker paused$/, "chat.action.paused"],
  [/^worker resumed$/, "chat.action.resumed"],
  [/^incident (\S+) filed.*$/, "chat.action.incident"],
  [/^supervisor pass started$/, "chat.action.supervisor"],
];

function actionLabel(a: string): string {
  for (const [re, key] of ACTIONS) {
    const m = a.match(re);
    if (m) return t(key, { id: m[1] ?? "" });
  }
  return a;
}

function CopyBtn({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setOk(true);
          setTimeout(() => setOk(false), 1200);
        });
      }}
      className="inline-flex items-center gap-1 hover:text-fg-2"
    >
      {ok ? <Check size={12} /> : <Copy size={12} />} {t(ok ? "common.copied" : "common.copy")}
    </button>
  );
}

function Thinking({ since: s, step, now }: { since: number | null; step: string | null; now: number }) {
  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center gap-3 text-[13px]">
      <Mark size={16} busy className="text-brand-ink" />
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={step ?? "pensando"}
          initial={{ opacity: 0, y: 3 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -3 }}
          transition={{ duration: 0.18 }}
          className="shimmer"
        >
          {step && step !== "Pensando" && step !== "Thinking" ? step : t("chat.thinking")}
        </motion.span>
      </AnimatePresence>
      <span className="tabular-nums text-fg-3">{t("chat.workingFor", { t: since(s, now) })}</span>
      <span className="h-px flex-1 bg-line" />
    </motion.div>
  );
}

import { ArrowUpRight, Check, Fingerprint, GitMerge } from "lucide-react";
import { useState } from "react";
import { mergePr, usePoll, type MergeState, type OwnPr } from "../api";
import { t } from "../i18n";
import { cx } from "../lib";
import { Spinner } from "./ui";

/** Ver (opens the PR in the browser) and Merge: green with a teammate's approve, orange with CodeRabbit's alone.
    The merge itself waits on his Touch ID in the guard. */
export function PrActions({ pr, size = "md" }: { pr: OwnPr; size?: "sm" | "md" }) {
  const [busy, setBusy] = useState(false);
  const { data: st } = usePoll<MergeState>(busy ? `/api/pr/${pr.repo}/${pr.number}` : null, 800);
  const state = busy ? st?.state ?? "waiting" : "open";
  const human = pr.approval === "human";
  const sm = size === "sm";

  const go = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setBusy(true);
    try {
      const r = await mergePr(pr.repo, pr.number);
      if (r.state === "error") throw new Error(r.error);
    } catch (err) {
      setBusy(false);
      alert(String(err).replace(/^Error: /, ""));
    }
  };

  if (state === "done") {
    return (
      <span className={cx("inline-flex items-center gap-1 text-green", sm ? "text-[12px]" : "text-[13px]")}>
        <Check size={sm ? 12 : 14} /> {t("pr.merged")}
      </span>
    );
  }
  const failed = state === "declined" || state === "error";
  return (
    <span className="inline-flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
      {!sm ? (
        <a
          href={pr.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-7 items-center gap-1 rounded-lg px-2.5 text-[13px] text-fg shadow-[0_0_0_0.5px_var(--line-strong)] transition hover:bg-hover"
        >
          {t("pr.view")} <ArrowUpRight size={13} className="opacity-60" />
        </a>
      ) : null}
      {state === "waiting" ? (
        <span className={cx("inline-flex items-center gap-1.5 text-fg-2", sm ? "text-[12px]" : "text-[13px]")}>
          <Spinner size={sm ? 11 : 13} /> <span className="shimmer">Touch ID</span>
        </span>
      ) : (
        <button
          onClick={go}
          title={t(human ? "pr.mergeTitle.human" : "pr.mergeTitle.bot")}
          className={cx(
            "inline-flex items-center gap-1 rounded-lg font-medium text-white transition hover:brightness-110 active:scale-[0.98]",
            sm ? "h-[22px] px-2 text-[12px]" : "h-7 px-2.5 text-[13px]",
            human ? "bg-[#1f9d55]" : "bg-[#e0700f]",
          )}
        >
          {sm ? <GitMerge size={11} /> : <Fingerprint size={13} />} Merge
        </button>
      )}
      {failed && !sm ? <span className="text-[12px] text-red">{st?.error}</span> : null}
    </span>
  );
}

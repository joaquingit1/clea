import { OctagonPause } from "lucide-react";
import { useState } from "react";
import { resumeProvider, type Board } from "../api";
import { t } from "../i18n";
import { href } from "../router";
import { BTN } from "./setup/fields";
import { Spinner } from "./ui";

/** The model provider refused a call for billing reasons (a key out of credit, a quota reached) and Clea stopped:
 *  no case runs and nothing is retried until the user says so. Said across the top of every screen, with the two ways
 *  out: try the same provider again, or choose another way to reach a model in Settings (which resumes by itself). */
export function Stopped({ stop, onResumed }: { stop: NonNullable<Board["health"]["stopped"]>; onResumed: () => void }) {
  const [busy, setBusy] = useState(false);
  const [still, setStill] = useState<string | null>(null);
  const resume = async () => {
    setBusy(true);
    setStill(null);
    try {
      const r = await resumeProvider();
      if (r.ok) onResumed();
      else setStill(r.why || t("stopped.still"));
    } catch (e) {
      setStill(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div role="alert" className="relative z-10 mx-3 mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl bg-amber-soft px-3.5 py-2.5 text-amber sm:mx-6">
      <OctagonPause size={17} className="shrink-0" />
      <div className="min-w-[220px] flex-1 text-[13px] leading-snug">
        <p className="font-semibold">{t("stopped.title")}</p>
        <p>{t("stopped.body", { why: still ?? stop.why })}</p>
        {still ? <p className="mt-0.5 font-medium">{t("stopped.stillRefusing")}</p> : null}
      </div>
      <a href={href({ page: "settings" })} className={BTN.secondary}>
        {t("stopped.change")}
      </a>
      <button type="button" onClick={resume} disabled={busy} className={BTN.primary}>
        {busy ? <Spinner size={13} className="text-white" /> : null}
        {t(busy ? "stopped.resuming" : "stopped.resume")}
      </button>
    </div>
  );
}

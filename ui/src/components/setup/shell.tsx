import { useEffect, useState, type ReactNode } from "react";
import { MARK } from "../../brand";
import { desktop } from "../../desktop";
import { t, type Key } from "../../i18n";
import { cx, loadSite } from "../../lib";
import { swap } from "../../router";
import { api, loadSetup, putSetup } from "../../setup";
import { Sky } from "../Sky";
import { AppIcon, Mark, Spinner } from "../ui";
import { BTN } from "./fields";

/** The whole window for what comes before the app: the sky, a strip to drag the window by (the traffic lights sit in it
 *  on the desktop), and the content in the middle. No sidebar: nothing to go to yet. */
export function Window({ children }: { children: ReactNode }) {
  return (
    <div className="relative isolate flex h-full flex-col overflow-hidden bg-bg">
      <Sky hidden={false} />
      <div data-drag className={cx("relative shrink-0", desktop ? "h-[46px]" : "h-3 sm:h-8")} />
      <div className="relative flex min-h-0 flex-1 items-center justify-center px-3 pb-3 sm:px-8 sm:pb-8">{children}</div>
    </div>
  );
}

/** The startup mark from index.html, for the moments React is already up and still waiting on the worker: same markup,
 *  same motion, so a retry looks like the launch and not like a new screen. */
export function Startup() {
  return (
    <div className="clea-startup" aria-label="Clea">
      <svg viewBox="-7 -7 14 14">
        <path d={MARK.outer} />
        <path d={MARK.inner} />
        <path d={MARK.dot} />
      </svg>
    </div>
  );
}

/** The worker cannot be reached. It usually comes up by itself, so the screen keeps asking quietly and goes away when
 *  it answers; the button is for the user who would rather not wait. */
export function NotRunning() {
  const [busy, setBusy] = useState(false);
  const [tries, setTries] = useState(0);
  const retry = async () => {
    setBusy(true);
    const [b] = await Promise.all([loadSetup(), new Promise((r) => setTimeout(r, 600))]); // long enough to see that it tried
    if (b.phase === "ready") await loadSite();
    setBusy(false);
    setTries((n) => n + 1);
  };
  useEffect(() => {
    const h = setInterval(async () => {
      if (document.hidden) return;
      if ((await loadSetup()).phase === "ready") loadSite();
    }, 4000);
    return () => clearInterval(h);
  }, []);
  return (
    <Window>
      <div className="page-in flex max-w-[400px] flex-col items-center px-4 text-center">
        <span className="grid size-16 place-items-center rounded-[20px] bg-hover text-fg-3">
          <Mark size={30} />
        </span>
        <h1 className="mt-6 text-[24px] leading-[1.875rem] font-semibold tracking-[-0.02em]">{t("down.title")}</h1>
        <p className="mt-2 text-[14px] leading-[1.5] text-pretty text-fg-2">{t("down.body")}</p>
        <button type="button" onClick={retry} disabled={busy} autoFocus className={cx(BTN.primary, "mt-6 min-w-[132px]")}>
          {busy ? <Spinner size={13} className="text-white" /> : null}
          {busy ? t("down.retrying") : t("common.retry")}
        </button>
        <p className={cx("mt-4 text-[12.5px] text-fg-3 transition-opacity duration-500", tries ? "opacity-100" : "opacity-0")} aria-hidden={!tries}>
          {t("down.hint")}
        </p>
      </div>
    </Window>
  );
}

const LINES: Key[] = ["starting.1", "starting.2", "starting.3", "starting.4"];

/** After "Start Clea": the worker restarts into normal mode. The mark works while the app asks every 1.5 s, and the
 *  board takes over the moment setup is complete and the worker is running. A request that fails here is the restart
 *  itself, so it is only asked again. */
export function Starting() {
  const [line, setLine] = useState(0);
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    let on = true;
    let h: ReturnType<typeof setTimeout>;
    const ask = async () => {
      try {
        const s = await api.state();
        if (on && s.complete && s.running) {
          await loadSite(); // the links of the workspace just set up
          swap({ page: "board" });
          putSetup(s);
          return;
        }
      } catch {
        /* restarting: ask again */
      }
      if (on) h = setTimeout(ask, 1500);
    };
    h = setTimeout(ask, 1500);
    const turn = setInterval(() => setLine((n) => (n + 1) % LINES.length), 2600);
    const late = setTimeout(() => setSlow(true), 45_000);
    return () => {
      on = false;
      clearTimeout(h);
      clearInterval(turn);
      clearTimeout(late);
    };
  }, []);
  return (
    <div className="page-in flex max-w-[400px] flex-col items-center px-4 text-center" role="status">
      <AppIcon size={72} busy />
      <h1 className="mt-7 text-[24px] leading-[1.875rem] font-semibold tracking-[-0.02em]">{t("starting.title")}</h1>
      <p key={line} className="fade-in mt-2 text-[14px]">
        <span className="shimmer">{t(LINES[line])}</span>
      </p>
      <p className={cx("mt-5 text-[12.5px] leading-snug text-fg-3 transition-opacity duration-500", slow ? "opacity-100" : "opacity-0")} aria-hidden={!slow}>
        {t("starting.slow")}
      </p>
    </div>
  );
}

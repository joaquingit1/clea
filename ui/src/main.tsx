import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { desktop } from "./desktop";
import { getLang } from "./i18n";
import { loadSite } from "./lib";
import { loadSetup } from "./setup";

try {
  const t = localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch {
  /* storage blocked: follow the system */
}

// film grain (Craft's WindowGrain): a 128 px tile of monochrome noise, drawn once, laid over the window by index.css
try {
  const n = 128, c = document.createElement("canvas");
  c.width = c.height = n;
  const g = c.getContext("2d")!, img = g.createImageData(n, n);
  for (let i = 0; i < n * n; i++) {
    const v = Math.random() * 255;
    img.data.set([v, v, v, 255], i * 4);
  }
  g.putImageData(img, 0, 0);
  document.documentElement.style.setProperty("--grain", `url(${c.toDataURL()})`);
} catch {
  /* no canvas: no grain */
}

if (desktop) {
  // the native window: room for the traffic lights, drag regions, the sidebar over macOS vibrancy
  document.documentElement.classList.add("desktop");
  document.documentElement.dataset.surface = "opaque";
  desktop.onSurface((s) => (document.documentElement.dataset.surface = s));
  const t = document.documentElement.dataset.theme;
  desktop.setTheme?.(t === "light" || t === "dark" ? t : "system");
}

// A new UI build (cd ui && pnpm build) reaches the open window by itself: every 20 s the page checks which bundle
// dist/index.html points at and, when it changed, reloads at the next moment nothing is being typed (2026-10-23: the
// sky was built but the open app kept showing the old screen until a manual Cmd-R).
const bundle = () => document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/"]')?.getAttribute("src") ?? null;
const current = bundle();
if (current) {
  setInterval(async () => {
    try {
      const html = await (await fetch("/index.html", { cache: "no-store" })).text();
      const next = html.match(/src="(\/assets\/index-[^"]+\.js)"/)?.[1];
      if (!next || next === current) return;
      const typing = [...document.querySelectorAll("textarea, input")].some((el) => (el as HTMLInputElement).value.trim());
      if (!typing) location.reload();
    } catch {
      /* the next check */
    }
  }, 20000);
}

// Before the first render: the workspace's links (GitHub org, Jira site), so no link starts out pointing nowhere, and
// where setup stands, so the first screen is the right one in the right language. Until both answer, the startup mark
// in index.html stays up. loadSetup never throws: a worker that cannot be reached is a screen of its own.
Promise.all([loadSite(), loadSetup()]).finally(() => {
  desktop?.setLang?.(getLang()); // the native menu follows the app's language from here on (i18n tells it of changes)
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});

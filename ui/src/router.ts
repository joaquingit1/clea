import { useEffect, useState } from "react";

export type Route =
  | { page: "board" }
  | { page: "threads" }
  | { page: "thread"; id: number }
  | { page: "pr"; repo: string; n: number }
  | { page: "chat"; conv: string | null }
  | { page: "activity" }
  | { page: "settings" }
  | { page: "setup"; step: string | null };

export function parse(hash: string): Route {
  const h = hash.replace(/^#\/?/, "");
  let m = h.match(/^pr\/([\w-]+)\/(\d+)/);
  if (m) return { page: "pr", repo: m[1], n: Number(m[2]) };
  m = h.match(/^t\/(\d+)/);
  if (m) return { page: "thread", id: Number(m[1]) };
  m = h.match(/^c\/(.+)$/);
  if (m) return { page: "chat", conv: m[1] === "new" ? null : decodeURIComponent(m[1]) };
  m = h.match(/^setup(?:\/([\w-]+))?/);
  if (m) return { page: "setup", step: m[1] ?? null };
  if (h.startsWith("settings")) return { page: "settings" };
  if (h.startsWith("hilos")) return { page: "threads" };
  if (h.startsWith("actividad")) return { page: "activity" };
  return { page: "board" };
}

export function href(r: Route): string {
  switch (r.page) {
    case "thread":
      return `#/t/${r.id}`;
    case "pr":
      return `#/pr/${r.repo}/${r.n}`;
    case "chat":
      return r.conv ? `#/c/${encodeURIComponent(r.conv)}` : "#/c/new";
    case "threads":
      return "#/hilos";
    case "activity":
      return "#/actividad";
    case "settings":
      return "#/settings";
    case "setup":
      return r.step ? `#/setup/${r.step}` : "#/setup";
    default:
      return "#/";
  }
}

export function go(r: Route) {
  window.location.hash = href(r);
}

/** Like go(), without a new history entry: for a redirect the Back button should not land on again. */
export function swap(r: Route) {
  window.history.replaceState(null, "", href(r));
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function useRoute(): Route {
  const [r, setR] = useState(() => parse(window.location.hash));
  useEffect(() => {
    const on = () => setR(parse(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return r;
}

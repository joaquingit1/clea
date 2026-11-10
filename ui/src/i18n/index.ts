import { Fragment, createElement, useSyncExternalStore, type ReactNode } from "react";
import { desktop } from "../desktop";
import { en } from "./en";
import { es } from "./es";

export type Lang = "en" | "es";
export type Key = keyof typeof en;
/** What a translation has to provide: every key of en.ts, nothing else. */
export type Messages = Record<Key, string>;
type Vars = Record<string, string | number>;
/** The keys that come as a.one /.other pair, without the suffix. */
export type PluralKey = Key extends infer K ? (K extends `${infer Base}.one` ? Base : never) : never;

const DICT: Record<Lang, Messages> = { en, es };
const LOCALE: Record<Lang, string> = { en: "en-US", es: "es-AR" };
const STORE = "lang";

/** Before setup says otherwise: the language chosen last time on this Mac, else the system's (Spanish or English). */
function initial(): Lang {
  try {
    const c = localStorage.getItem(STORE);
    if (c === "en" || c === "es") return c;
  } catch {
    /* storage blocked: follow the system */
  }
  return (navigator.language || "").toLowerCase().startsWith("es") ? "es" : "en";
}

let lang: Lang = initial();
const subs = new Set<() => void>();
document.documentElement.lang = lang;

export function getLang(): Lang {
  return lang;
}

/** Switches the whole app at once: everything under useLang() renders again, and the choice is cached so the next launch
 *  paints in the right language before the worker has answered. */
export function setLang(next: Lang): void {
  if (next !== "en" && next !== "es") return;
  try {
    localStorage.setItem(STORE, next);
  } catch {
    /* fine: this session only */
  }
  if (next === lang) return;
  lang = next;
  document.documentElement.lang = next;
  desktop?.setLang?.(next); // the native menu is written in the same language
  subs.forEach((f) => f());
}

function subscribe(f: () => void): () => void {
  subs.add(f);
  return () => subs.delete(f);
}

/** The current language. The root calls it, so a switch re-renders every screen; anything that memoizes text takes the
 *  value as a dependency. */
export function useLang(): Lang {
  return useSyncExternalStore(subscribe, getLang);
}

export function t(key: Key, vars?: Vars): string {
  const s = DICT[lang][key] ?? en[key] ?? key;
  return vars ? s.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : s;
}

/** A counted phrase: "1 case" or "3 cases". `{n}` is the count. */
export function tn(key: PluralKey, n: number, vars?: Vars): string {
  return t(`${key}.${n === 1 ? "one" : "other"}` as Key, { n, ...vars });
}

/** A sentence with an element inside it (a command in code type, a link): each `{name}` becomes the node given. */
export function rich(key: Key, nodes: Record<string, ReactNode>): ReactNode {
  const parts = t(key).split(/\{(\w+)\}/g);
  return createElement(Fragment, null, ...parts.map((p, i) => (i % 2 ? (nodes[p] ?? `{${p}}`) : p)));
}

/** "a, b and c" the way the language writes it. */
export function listOf(items: string[]): string {
  return new Intl.ListFormat(lang, { style: "long", type: "conjunction" }).format(items);
}

/** The locale for dates and numbers. */
export function locale(): string {
  return LOCALE[lang];
}

/** The Electron bridge (desktop/preload.cjs). In a browser tab it is absent and every call is a no-op. */
interface Bridge {
  desktop: true;
  platform: string;
  setBadge: (n: number) => void;
  setTheme?: (t: "light" | "dark" | "system") => void;
  /** the app's language, so the native menu and the About panel are written in it too */
  setLang?: (l: "en" | "es") => void;
  onCommand: (cb: (c: string) => void) => void;
  onSurface: (cb: (s: string) => void) => void;
}

export const desktop: Bridge | null = (window as unknown as { clea?: Bridge }).clea ?? null;
export const isDesktop = !!desktop;

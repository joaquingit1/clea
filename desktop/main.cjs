// Clea desktop: the agent's console as a native macOS app (the user, 2026-10-20: "you should make an electron app,
// that's why i asked you to use rea. to see how they make electron efficient, and their tricks").
//
// The tricks come from the Codex desktop app (com.openai.codex 26.908, read statically from its app.asar):
// - the UI is served from disk through a privileged app:// scheme, not file:// and not a localhost page;
// - one instance; a second launch focuses the first;
// - hidden inset title bar, traffic lights at (16, 16), acceptFirstMouse, a transparent window with "menu" vibrancy
//   behind the sidebar, switched to an opaque surface when the window loses focus or is 4K-sized (vibrancy costs GPU);
// - backgroundColor set and the window shown on ready-to-show, so there is no white flash;
// - bounds persisted and restored clamped to a visible display, minimum 480x600;
// - contextIsolation, sandboxed preload, no Node in the renderer; links open in the browser.
// The data stays in the harness: /api/* on app:// is proxied to the worker (127.0.0.1:4848), so the UI code is the same
// in the browser and here, and the worker remains the only thing that reads the DB or talks to the orchestrator.
require("node:module").enableCompileCache?.(); // V8 code cache for the main process: faster second launch

const { app, BrowserWindow, Menu, Notification, ipcMain, nativeTheme, net, protocol, screen, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { pathToFileURL } = require("node:url");

const API = process.env.CLEA_API || "http://127.0.0.1:4848";
// The engine this app runs itself, when it has one: CLEA_ENGINE names its folder, or the packaged app ships it in
// Resources/engine. The first install has neither (its worker is a launchd job) and the app only talks to it.
const ENGINE =
  process.env.CLEA_ENGINE ||
  (process.resourcesPath && fs.existsSync(path.join(process.resourcesPath, "engine", "ap")) ? path.join(process.resourcesPath, "engine") : null);
// the built UI lives with the engine: `cd ui && pnpm build` updates the app on its next reload (⌘R)
const DIST = process.env.CLEA_UI || path.join(ENGINE || path.join(app.getPath("home"), ".claude", "autopilot", "v2"), "ui", "dist");
const ORIGIN = "app://-";
const MIN = { width: 480, height: 600 };
const OPAQUE_PX = { long: 3840, short: 2160 }; // Codex: past this physical size the vibrancy blur is not worth the GPU
const STATE = () => path.join(app.getPath("userData"), "window.json");

// Clea everywhere, never "Electron" (Codex ships a rebuilt Electron whose framework and helpers carry its name; with the
// stock build the packager names the executable and helpers "Clea", and this covers what the process says at runtime)
app.setName("Clea");
// CLEA_USER_DATA: a separate profile (and single-instance lock) for test runs while his Clea is open, like Codex's
// CODEX_ELECTRON_USER_DATA_PATH
app.setPath("userData", process.env.CLEA_USER_DATA || path.join(app.getPath("appData"), "Clea"));
process.title = "Clea";

// ---- the engine, when the app is what runs it ---------------------------------------------------------------------------
// `python -m ap.cli run` is one install kept alive: its guard and its worker, restarted by ap/launch.py when either
// stops. The app starts it at launch and stops it on quit; if it ever exits, it is started again with a growing wait.
// The install's own files (config, database, logs) live in the app's data folder, never inside the app bundle.
const INSTALL = process.env.CLEA_HOME || path.join(app.getPath("userData"), "install");
// a Mac app starts with a bare PATH: the tools the engine calls (git, gh, claude, codex) live in these
const TOOLS = ["/opt/homebrew/bin", "/usr/local/bin", ".local/bin", ".bun/bin", ".npm-global/bin", ".claude/local"]
  .map((p) => (p.startsWith("/") ? p : path.join(app.getPath("home"), p)))
  .join(":");
let engine = null;
let quitting = false;
let engineFails = 0;

function enginePython() {
  const found = [process.env.CLEA_PYTHON, path.join(ENGINE, ".venv", "bin", "python"), path.join(ENGINE, "venv", "bin", "python")].find(
    (p) => p && fs.existsSync(p),
  );
  return found || "python3";
}

async function engineAnswers() {
  try {
    return (await net.fetch(`${API}/api/setup`)).status < 500;
  } catch {
    return false;
  }
}

async function startEngine() {
  if (!ENGINE || engine || quitting) return;
  // something already answers there (the engine of an app that crashed, or one the user started by hand): use it
  if (await engineAnswers()) return;
  const logs = path.join(INSTALL, "var", "log");
  fs.mkdirSync(logs, { recursive: true });
  const out = fs.openSync(path.join(logs, "engine.out"), "a");
  const started = Date.now();
  const child = spawn(enginePython(), ["-m", "ap.cli", "run"], {
    cwd: ENGINE,
    stdio: ["ignore", out, out],
    env: {
      ...process.env,
      CLEA_HOME: INSTALL,
      CLEA_CODE: ENGINE,
      CLEA_PORT: new URL(API).port || "4848",
      PYTHONUNBUFFERED: "1",
      PATH: `${TOOLS}:${process.env.PATH || "/usr/bin:/bin:/usr/sbin:/sbin"}`,
    },
  });
  fs.closeSync(out);
  engine = child;
  const again = () => {
    if (engine !== child) return;
    engine = null;
    if (quitting) return;
    engineFails = Date.now() - started > 120_000 ? 0 : engineFails + 1; // a real run starts the count again
    setTimeout(startEngine, Math.min(30_000, 1000 * 2 ** engineFails));
  };
  child.on("error", again); // no Python to run it with: the page says Clea is not running, and this keeps trying
  child.on("exit", again);
}

function stopEngine() {
  quitting = true;
  try {
    engine?.kill("SIGTERM"); // ap/launch.py stops the worker and the guard, then exits
  } catch {}
}
app.userAgentFallback = app.userAgentFallback.replace(/\s*Electron\/\S+/, "").replace(/\bclea\/\S+/i, `Clea/${app.getVersion()}`);

// Every string the native side shows (the menus, the About panel, the two messages a broken install can produce), in
// the app's two languages. The page owns the language (ui/src/i18n) and tells this process through the bridge; until
// it has, the worker's setup state says it, and before setup is done the system's language does.
const STR = {
  en: {
    tagline: "An engineering agent that works alongside you",
    about: "About Clea", settings: "Settings…", services: "Services", hide: "Hide Clea", hideOthers: "Hide others", unhide: "Show all", quit: "Quit Clea",
    file: "File", newChat: "New chat", search: "Search", closeWindow: "Close window",
    edit: "Edit", undo: "Undo", redo: "Redo", cut: "Cut", copy: "Copy", paste: "Paste", pasteMatch: "Paste and match style", selectAll: "Select all",
    go: "Go", home: "Home", threads: "Threads", activity: "Activity", back: "Back", forward: "Forward",
    view: "View", reload: "Reload", devTools: "Developer tools", actualSize: "Actual size", zoomIn: "Zoom in", zoomOut: "Zoom out", fullScreen: "Full screen",
    window: "Window", minimize: "Minimize", zoom: "Zoom", front: "Bring all to front",
    down: (api, why) => `Clea is not answering (${api}): ${why}`,
    noBuild: "The UI build is missing:",
  },
  es: {
    tagline: "Un agente de ingeniería que trabaja a la par tuya",
    about: "Acerca de Clea", settings: "Ajustes…", services: "Servicios", hide: "Ocultar Clea", hideOthers: "Ocultar otros", unhide: "Mostrar todo", quit: "Salir de Clea",
    file: "Archivo", newChat: "Chat nuevo", search: "Buscar", closeWindow: "Cerrar ventana",
    edit: "Edición", undo: "Deshacer", redo: "Rehacer", cut: "Cortar", copy: "Copiar", paste: "Pegar", pasteMatch: "Pegar con el mismo estilo", selectAll: "Seleccionar todo",
    go: "Ir", home: "Inicio", threads: "Hilos", activity: "Actividad", back: "Atrás", forward: "Adelante",
    view: "Ver", reload: "Recargar", devTools: "Herramientas de desarrollo", actualSize: "Tamaño real", zoomIn: "Acercar", zoomOut: "Alejar", fullScreen: "Pantalla completa",
    window: "Ventana", minimize: "Minimizar", zoom: "Zoom", front: "Traer todo al frente",
    down: (api, why) => `el harness no responde (${api}): ${why}`,
    noBuild: "Falta el build de la UI:",
  },
};
let lang = "en"; // the system's language once the app is ready (app.getLocale() is not reliable before that)

function about() {
  app.setAboutPanelOptions({
    applicationName: "Clea",
    applicationVersion: app.getVersion(),
    version: "",
    copyright: "© 2026 Joaquin Castellano",
    credits: STR[lang].tagline,
  });
}
about();

/** The app changed language (or said which one it is in): the menu and the About panel are written again. */
function setLanguage(next) {
  if (!STR[next] || next === lang) return;
  lang = next;
  about();
  menu();
}

/** Once at launch, so the menu is right before the page has loaded: the language setup has on record, when the user
 *  has chosen one (the welcome step is saved). A worker that is down or predates setup leaves the system's language. */
async function learnLanguage() {
  try {
    const r = await net.fetch(`${API}/api/setup`);
    if (!r.ok) return;
    const s = await r.json();
    if (s.complete || s.steps?.some((x) => x.id === "welcome" && x.done)) setLanguage(s.language);
  } catch {} // the page tells us as soon as it is up
}

protocol.registerSchemesAsPrivileged([
  { scheme: "app", privileges: { standard: true, secure: true, stream: true, supportFetchAPI: true } },
]);

if (!app.requestSingleInstanceLock()) {
  app.exit(0);
}

let win = null;

function readBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(STATE(), "utf8"));
    const area = screen.getDisplayMatching(b).workArea;
    const visible = b.x < area.x + area.width - 80 && b.y < area.y + area.height - 80 && b.x + b.width > area.x + 80 && b.y >= area.y - 10;
    if (!visible) return null;
    return { ...b, width: Math.max(b.width, MIN.width), height: Math.max(b.height, MIN.height) };
  } catch {
    return null;
  }
}

function saveBounds() {
  if (!win || win.isDestroyed()) return;
  try {
    fs.writeFileSync(STATE(), JSON.stringify({ ...win.getNormalBounds(), maximized: win.isMaximized() }));
  } catch {
    /* next close saves it */
  }
}

function surface(w) {
  // vibrant while focused and not huge, opaque otherwise (Codex's D6e/T6e rule)
  const b = w.getBounds();
  const f = screen.getDisplayMatching(b).scaleFactor;
  const huge = Math.max(b.width, b.height) * f >= OPAQUE_PX.long && Math.min(b.width, b.height) * f >= OPAQUE_PX.short;
  return !w.isFocused() || huge ? "opaque" : "vibrant";
}

function applyBackdrop(w, force) {
  if (!w || w.isDestroyed()) return;
  const s = surface(w);
  if (!force && w.__surface === s) return;
  w.__surface = s;
  const dark = nativeTheme.shouldUseDarkColors;
  w.setBackgroundColor(s === "opaque" ? (dark ? "#141414" : "#f6f6f6") : "#00000000");
  w.setVibrancy(s === "opaque" ? null : "menu");
  w.webContents.send("surface", s);
}

function createWindow() {
  const saved = readBounds();
  const dark = nativeTheme.shouldUseDarkColors;
  win = new BrowserWindow({
    width: saved?.width ?? 1440,
    height: saved?.height ?? 900,
    ...(saved ? { x: saved.x, y: saved.y } : {}),
    minWidth: MIN.width,
    minHeight: MIN.height,
    title: "Clea",
    show: false,
    backgroundColor: dark ? "#141414" : "#f6f6f6",
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 16, y: 16 },
    acceptFirstMouse: true,
    vibrancy: "menu",
    visualEffectState: "followWindow",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: true,
      scrollBounce: false, // the window's content never rubber-bands past its edges
      devTools: true,
    },
  });
  if (saved?.maximized) win.maximize();

  win.once("ready-to-show", () => {
    applyBackdrop(win, true);
    win.show();
  });
  win.on("focus", () => applyBackdrop(win));
  win.on("blur", () => applyBackdrop(win));
  win.on("resize", () => applyBackdrop(win));
  win.on("close", saveBounds);
  win.on("closed", () => (win = null));

  const wc = win.webContents;
  wc.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  wc.on("will-navigate", (e, url) => {
    if (!url.startsWith(ORIGIN)) {
      e.preventDefault();
      if (/^https?:/.test(url)) shell.openExternal(url);
    }
  });
  wc.on("did-finish-load", () => applyBackdrop(win, true));
  win.loadURL(`${ORIGIN}/index.html`);
}

async function serve(req) {
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) {
    try {
      const init = { method: req.method, headers: req.headers };
      if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.arrayBuffer();
      return await net.fetch(API + url.pathname + url.search, init);
    } catch (e) {
      return Response.json({ error: STR[lang].down(API, e.message) }, { status: 503 });
    }
  }
  const root = path.resolve(DIST);
  let file = path.resolve(root, "." + decodeURIComponent(url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(root, "index.html"); // client-side routes
  }
  if (!fs.existsSync(file)) {
    return new Response(`<meta charset="utf-8"><p style='font:14px -apple-system;padding:24px'>${STR[lang].noBuild} <code>cd ${path.dirname(root)} && pnpm build</code></p>`, {
      headers: { "content-type": "text/html" },
    });
  }
  return net.fetch(pathToFileURL(file).toString());
}

/** The application menu in the app's language. Built again whenever that changes. */
function menu() {
  const send = (c) => () => win?.webContents.send("command", c);
  const L = STR[lang];
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      {
        label: "Clea",
        submenu: [
          { role: "about", label: L.about },
          { type: "separator" },
          { label: L.settings, accelerator: "CmdOrCtrl+,", click: send("settings") },
          { type: "separator" },
          { role: "services", label: L.services },
          { type: "separator" },
          { role: "hide", label: L.hide },
          { role: "hideOthers", label: L.hideOthers },
          { role: "unhide", label: L.unhide },
          { type: "separator" },
          { role: "quit", label: L.quit },
        ],
      },
      {
        label: L.file,
        submenu: [
          { label: L.newChat, accelerator: "CmdOrCtrl+N", click: send("new-chat") },
          { label: L.search, accelerator: "CmdOrCtrl+K", click: send("palette") },
          { type: "separator" },
          { role: "close", label: L.closeWindow },
        ],
      },
      {
        label: L.edit,
        submenu: [
          { role: "undo", label: L.undo },
          { role: "redo", label: L.redo },
          { type: "separator" },
          { role: "cut", label: L.cut },
          { role: "copy", label: L.copy },
          { role: "paste", label: L.paste },
          { role: "pasteAndMatchStyle", label: L.pasteMatch },
          { role: "selectAll", label: L.selectAll },
        ],
      },
      {
        label: L.go,
        submenu: [
          { label: L.home, accelerator: "CmdOrCtrl+1", click: send("board") },
          { label: L.threads, accelerator: "CmdOrCtrl+2", click: send("threads") },
          { label: L.activity, accelerator: "CmdOrCtrl+3", click: send("activity") },
          { type: "separator" },
          { label: L.back, accelerator: "CmdOrCtrl+[", click: () => win?.webContents.navigationHistory.goBack() },
          { label: L.forward, accelerator: "CmdOrCtrl+]", click: () => win?.webContents.navigationHistory.goForward() },
        ],
      },
      {
        label: L.view,
        submenu: [
          { role: "reload", label: L.reload },
          { role: "toggleDevTools", label: L.devTools },
          { type: "separator" },
          { role: "resetZoom", label: L.actualSize },
          { role: "zoomIn", label: L.zoomIn },
          { role: "zoomOut", label: L.zoomOut },
          { type: "separator" },
          { role: "togglefullscreen", label: L.fullScreen },
        ],
      },
      {
        label: L.window,
        role: "window",
        submenu: [{ role: "minimize", label: L.minimize }, { role: "zoom", label: L.zoom }, { type: "separator" }, { role: "front", label: L.front }],
      },
    ]),
  );
}

ipcMain.on("lang", (e, l) => {
  // the page's language is the app's: the menu and the About panel follow it
  if (e.senderFrame?.url?.startsWith(ORIGIN) !== true) return;
  setLanguage(String(l));
});

ipcMain.on("theme", (e, t) => {
  // the app's light/dark switch drives the window's appearance too, so the vibrancy material matches the page
  if (e.senderFrame?.url?.startsWith(ORIGIN) !== true) return;
  if (["light", "dark", "system"].includes(t)) nativeTheme.themeSource = t;
});

ipcMain.on("badge", (e, n) => {
  if (e.senderFrame?.url?.startsWith(ORIGIN) !== true) return; // only our page sets the Dock badge
  const k = Number.isFinite(n) && n > 0 ? String(Math.min(n, 999)) : "";
  app.dock?.setBadge(k);
});

// ready-to-merge PRs as macOS notifications (the user, 2026-10-22: "notify me as a Mac push notification when one is
// ready"). The worker queues them (babysit.mac_notify); the app shows each once and remembers the last id it showed. A
// first run only shows what is under an hour old, never the backlog. A click opens the PR, or the app for a batch.
const NOTIFIED = () => path.join(app.getPath("userData"), "notified.json");
const shown = [];  // a Notification that gets garbage-collected loses its click handler
async function pollNotifications() {
  let last = null;
  try {
    last = JSON.parse(fs.readFileSync(NOTIFIED(), "utf8")).last;
  } catch {}
  try {
    const r = await net.fetch(`${API}/api/notifications?after=${last || 0}`);
    if (!r.ok) return;
    const items = await r.json();
    for (const x of items) {
      if (last !== null || Date.now() / 1000 - x.at < 3600) {
        const n = new Notification({ title: "Clea", subtitle: x.subtitle, body: x.body, sound: "Glass" });
        n.on("click", () => (x.url ? shell.openExternal(x.url) : win ? win.show() : createWindow()));
        n.show();
        shown.push(n);
        if (shown.length > 20) shown.shift();
      }
      last = Math.max(last || 0, x.id);
    }
    if (last !== null) fs.writeFileSync(NOTIFIED(), JSON.stringify({ last }));
  } catch {} // worker down or restarting: the next poll catches up
}

app.on("second-instance", () => {
  if (!win) return createWindow();
  if (win.isMinimized()) win.restore();
  win.focus();
});

app.whenReady().then(() => {
  if (process.env.CLEA_THEME) nativeTheme.themeSource = process.env.CLEA_THEME; // screenshot checks
  protocol.handle("app", serve);
  nativeTheme.on("updated", () => applyBackdrop(win, true));
  lang = /^es/i.test(app.getLocale()) ? "es" : "en";
  about();
  menu();
  startEngine(); // only when this app runs its own engine; the page waits for it and shows that it is starting
  learnLanguage();
  createWindow();
  pollNotifications();
  setInterval(pollNotifications, 30_000);
  app.on("activate", () => {
    if (!win) createWindow();
    else win.show();
  });
});

app.on("before-quit", stopEngine);
// a quit that arrives as a signal (logout, `kill`) goes through the same door, so the engine never outlives the app
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => app.quit());

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

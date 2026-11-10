// The only bridge between the page and the app: no Node, no raw ipcRenderer, a few narrow calls.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("clea", {
  desktop: true,
  platform: process.platform,
  setBadge: (n) => ipcRenderer.send("badge", Number(n) || 0),
  setTheme: (t) => ipcRenderer.send("theme", String(t)),
  setLang: (l) => ipcRenderer.send("lang", String(l)),
  onCommand: (cb) => ipcRenderer.on("command", (_e, c) => cb(String(c))),
  onSurface: (cb) => ipcRenderer.on("surface", (_e, s) => cb(String(s))),
});

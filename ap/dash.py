"""Local dashboard: "a loading bar / state for each concurrent thread, so we can see how long
each takes and if any stalls; a local visualization where we could see the state of things, grouped, also for
debugging". Zero tokens: rendered from the DB every 30 s to var/dashboard.html and served on 127.0.0.1:4747."""
from __future__ import annotations

import html
import http.server
import json
import threading
import time

from . import db, site
from .core import VAR, log, now

PORT = 4747
OUT = VAR / "dashboard.html"
_server_started = False
ACTIVE_STATES = ("investigate", "telemetry", "plan", "critique", "build", "gate", "self_find", "self_review", "fix", "publish",
                 "ci_wait", "ci_fix", "verify_done", "pr_body_refresh", "announce", "announce_check", "waiting", "vet", "propose",
                 "propose_check", "reply_draft", "reply_check", "comments_triage", "comments_verify", "comments_reply",
                 "review_prepare", "review_find", "review_verify", "review_post", "loop_track")


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _age(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


def _bar(frac: float, color: str = "#4a90e2") -> str:
    pct = max(2, min(100, int(frac * 100)))
    return f'<div class="bar"><div style="width:{pct}%;background:{color}"></div></div>'


def render() -> str:
    from . import hot
    t = now()
    day = int(time.mktime(time.strptime(db.today(), "%Y-%m-%d")))
    rows = []
    # 1. threads
    threads = []
    for r in db.q("SELECT k FROM kv WHERE k LIKE 'thread_beat:%' ORDER BY k"):
        name = r["k"].split(":", 1)[1]
        st = db.kv_get(f"stage_started:{name}") or {}
        beat = db.kv_get(r["k"], 0) or 0
        if st.get("item"):
            row = db.q1("SELECT title, claimed_at FROM items WHERE id=?", st["item"])
            beat = max(beat, (row["claimed_at"] if row and row["claimed_at"] else 0))
            budget = hot.STAGE_MAX.get(st.get("status"), hot.STALL)
            el = t - st["at"]
            color = "#d9534f" if el > budget else "#f0ad4e" if el > budget * 0.7 else "#5cb85c"
            threads.append(f"<tr><td>{_esc(name)}</td><td>#{st['item']} {_esc((row['title'] if row else '')[:60])}</td>"
                           f"<td>{_esc(st.get('status'))}</td><td>{_age(el)} / {_age(budget)}</td><td>{_bar(el / budget, color)}</td>"
                           f"<td>{_age(t - beat)} ago</td></tr>")
        else:
            threads.append(f"<tr class=idle><td>{_esc(name)}</td><td colspan=4>idle</td><td>{_age(t - beat)} ago</td></tr>")
    rows.append("<h2>Threads</h2><table><tr><th>thread</th><th>item</th><th>stage</th><th>elapsed / budget</th><th></th><th>beat</th></tr>"
                + "".join(threads) + "</table>")
    # 2. cases by group
    groups = {"Construyendo": [], "Reviews del squad": [], "Respuestas": [], "PRs propios (CI/threads)": [], "Esperando": [], "Otros": []}
    for it in db.q("SELECT id, kind, status, title, created_at, updated_at, claimed_by, last_error, data FROM items WHERE status IN "
                   f"({','.join('?' * len(ACTIVE_STATES))}) ORDER BY updated_at", *ACTIVE_STATES):
        d = json.loads(it["data"] or "{}")
        calls = db.q1("SELECT count(*) n, round(coalesce(sum(usd),0),1) u FROM calls WHERE item_id=? AND tier='claude'", it["id"])
        note = (db.q1("SELECT note FROM transitions WHERE item_id=? ORDER BY at DESC LIMIT 1", it["id"]) or {"note": ""})["note"] or ""
        prs = ", ".join(f"<a href='{p['pr_url']}'>{p['repo']}#{p['pr']}</a>" for p in d.get("parts", []) if p.get("pr_url"))
        idle = t - it["updated_at"]
        cls = "stale" if idle > 3 * 3600 else ""
        line = (f"<tr class='{cls}'><td>#{it['id']}</td><td>{_esc(it['kind'])}</td><td><b>{_esc(it['status'])}</b></td>"
                f"<td>{_esc(it['title'][:70])}</td><td>{_age(t - it['created_at'])}</td><td>{_age(idle)}</td>"
                f"<td>{_esc(it['claimed_by'] or '')}</td><td>{calls['n']} / ${calls['u']}</td><td>{prs}</td>"
                f"<td class=small>{_esc(note[:70])} {_esc((it['last_error'] or '')[:80])}</td></tr>")
        g = ("PRs propios (CI/threads)" if it["kind"] in ("CI", "COMMENTS") else "Reviews del squad" if it["kind"] == "REVIEW" else
             "Respuestas" if it["kind"] == "REPLY" else "Esperando" if it["status"] in ("waiting", "ci_wait") else
             "Construyendo" if it["status"] in ("investigate", "telemetry", "plan", "critique", "build", "gate", "self_find", "self_review",
                                                 "fix", "publish", "ci_fix", "verify_done", "pr_body_refresh", "announce", "announce_check") else "Otros")
        groups[g].append(line)
    # cases waiting on him (asks) and those closed today: they must not vanish from the view
    waiting = []
    for it in db.q("SELECT id, kind, status, title, created_at, updated_at, data FROM items WHERE status IN ('awaiting_approval','awaiting_human') ORDER BY updated_at DESC"):
        d = json.loads(it["data"] or "{}")
        ask = db.q1("SELECT ref FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL ORDER BY id DESC", it["id"])
        link = f"<a href='{site.slack_link(db.kv_get('self_dm_channel') or '', ask['ref'])}'>ask</a>" if ask and ask["ref"] else ""
        waiting.append(f"<tr><td>#{it['id']}</td><td>{_esc(it['kind'])}</td><td><b>{_esc(it['status'])}</b></td><td>{_esc(it['title'][:70])}</td>"
                       f"<td>{_age(t - it['created_at'])}</td><td>{_age(t - it['updated_at'])}</td><td>{link}</td></tr>")
    if waiting:
        rows.append(f"<h2>Esperando tu ✅ · {len(waiting)}</h2><table><tr><th>id</th><th>kind</th><th>estado</th><th>title</th><th>age</th><th>since</th><th></th></tr>{''.join(waiting)}</table>")
    closed = []
    for it in db.q("SELECT id, kind, status, title, updated_at FROM items WHERE status IN ('done','dropped','expired','failed') AND updated_at>=? ORDER BY updated_at DESC LIMIT 40", day):
        note = (db.q1("SELECT note FROM transitions WHERE item_id=? ORDER BY at DESC LIMIT 1", it["id"]) or {"note": ""})["note"] or ""
        closed.append(f"<tr class=idle><td>#{it['id']}</td><td>{_esc(it['kind'])}</td><td>{_esc(it['status'])}</td><td>{_esc(it['title'][:70])}</td>"
                      f"<td>{time.strftime('%H:%M', time.localtime(it['updated_at']))}</td><td class=small>{_esc(note[:90])}</td></tr>")
    if closed:
        rows.append(f"<h2>Cerrados hoy · {len(closed)}</h2><table><tr><th>id</th><th>kind</th><th>estado</th><th>title</th><th>hora</th><th>por qué</th></tr>{''.join(closed)}</table>")
    for g, lines in groups.items():
        if lines:
            rows.append(f"<h2>{g} · {len(lines)}</h2><table><tr><th>id</th><th>kind</th><th>stage</th><th>title</th><th>age</th><th>idle</th>"
                        f"<th>thread</th><th>calls / $</th><th>PRs</th><th>last</th></tr>{''.join(lines)}</table>")
    # 3. limits, spend, reaper, reload
    pu = db.kv_get("claude_paused_until", 0) or 0
    spend = db.q("SELECT strftime('%H', at,'unixepoch','localtime') h, round(sum(usd),1) u, count(*) n FROM calls WHERE tier='claude' AND at>=? GROUP BY h", day)
    total = db.q1("SELECT round(coalesce(sum(usd),0),1) u, count(*) n FROM calls WHERE tier='claude' AND at>=?", day)
    hr = db.kv_get("hot_reload") or {}
    wd = db.kv_get("watchdog") or {}
    per_hour = " · ".join(f"{s['h']}h ${s['u']}" for s in spend)
    resumable = ", ".join(_esc(r["k"][7:]) for r in db.q("SELECT k FROM kv WHERE k LIKE 'resume:%' AND v!='null'"))
    rows.append("<h2>Sistema</h2><ul>"
                + (f"<li class=warn>Claude en pausa por límite hasta {time.strftime('%H:%M', time.localtime(pu))}</li>" if pu > t else "<li>Claude: sin pausa</li>")
                + f"<li>Gasto hoy: ${total['u']} en {total['n']} llamadas · por hora: {per_hour}</li>"
                + f"<li>Hot reload: rev {_esc(hr.get('rev', '?'))} a las {time.strftime('%H:%M', time.localtime(hr.get('at', 0))) if hr.get('at') else '-'}"
                + (f" · fallos: {_esc(hr.get('failed'))}" if hr.get("failed") else "") + "</li>"
                + f"<li>Watchdog: {_esc(wd.get('problems') or 'sin problemas')} · {_esc(wd.get('pauses') or '')}</li>"
                + f"<li>Resumibles: {resumable or 'ninguno'}</li>"
                + "</ul>")
    reaper = [l for l in _tail_log(400) if "reaper:" in l or "hot reload" in l or "resuming interrupted" in l or "session kept" in l][-12:]
    rows.append("<h2>Reaper / reload (últimas)</h2><pre>" + _esc("\n".join(x[:160] for x in reaper)) + "</pre>")
    stages = [l for l in _tail_log(300) if "] stage {" in l][-20:]
    rows.append("<h2>Últimos stages</h2><pre>" + _esc("\n".join(x[11:150] for x in stages)) + "</pre>")
    page = ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30><title>autopilot</title>"
            "<style>body{font:13px -apple-system,Helvetica,sans-serif;margin:16px;background:#111;color:#ddd}h2{margin:18px 0 6px;font-size:15px;color:#9cf}"
            "table{border-collapse:collapse;width:100%}td,th{padding:3px 6px;border-bottom:1px solid #333;text-align:left;vertical-align:top}th{color:#888;font-weight:normal}"
            ".bar{width:140px;height:8px;background:#333;border-radius:4px;overflow:hidden}.bar div{height:8px}.idle td{color:#666}.stale td{color:#f0ad4e}"
            ".small{color:#999;font-size:11px}.warn{color:#d9534f}pre{background:#181818;padding:8px;font-size:11px;white-space:pre-wrap}a{color:#9cf}</style>"
            f"<h1 style='font-size:17px'>autopilot · {time.strftime('%a %d %H:%M:%S')}</h1>" + "".join(rows))
    OUT.write_text(page)
    return page


def _tail_log(n: int) -> list[str]:
    from .core import LOG_DIR
    import datetime as dt
    f = LOG_DIR / f"ap-{dt.date.today():%F}.log"
    try:
        return f.read_text(errors="ignore").splitlines()[-n:]
    except Exception:
        return []


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = OUT.read_bytes() if OUT.exists() else b"<p>rendering...</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


def serve_once() -> None:
    global _server_started
    if _server_started or not db.is_live():  # never the selftest's scratch DB (db.is_live)
        return
    _server_started = True
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _H)
        threading.Thread(target=srv.serve_forever, name="dash-http", daemon=True).start()
        log("dashboard serving", url=f"http://127.0.0.1:{PORT}")
    except OSError as e:
        log("dashboard port busy", err=str(e)[:80])

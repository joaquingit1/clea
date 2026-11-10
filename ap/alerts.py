"""Alert-pattern ledger (ported from v1, 2026-08-11/14). Zero tokens to count.

Every alert from #squad-sis-alerts, in or out of the fence, updates a fingerprint entry
(service | route | error class). Escalation, deterministic:
- a NEW fingerprint carrying a real error body inside the fence -> one telemetry-first
  INCIDENT now (the "IDEA 500" shape the user praised), never a second while it is open;
- any fingerprint crossing 3 hits in 24h or 10 in 7d -> surfaced: an INCIDENT regardless of
  domain (out-of-fence ones end in a routing draft for the owning team);
- surfaced: no re-report unless the count tripled or it went quiet 3+ days and came back;
- mitigated (a fix of ours merged): keep counting, escalate again only if it continues 48h later.
"""
from __future__ import annotations

import re

from . import db, site
from .core import log, now

SERVICE = re.compile(r"^\s*\[(\w[\w-]*)\]")
ROUTE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w./-]*)")
STATUS = re.compile(r"\b(4\d\d|5\d\d)\b")
EXC = re.compile(r"\b([A-Z][A-Za-z]+(?:Error|Exception|Timeout|Violation|Denied))\b")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b\d{3,}\b")
BODY = re.compile(r"Traceback|Exception|Error\b|IntegrityError|UniqueViolation|status=5\d\d|\b5\d\d\b|detail", re.I)


DD_STATE = re.compile(r"^\W*(?:re-?)?(?:triggered|warn(?:ing)?|recovered|alert|no data)\b[^:]*:\s*", re.I)
TITLE = re.compile(r"^\s*(?:\[[^\]]+\])+\s*([^\n/]{4,})")


def fingerprint(text: str) -> str:
    """service|route|error. Datadog monitor alerts ("Warn: [schools][prd] Memoria alta en...") carry the service after
    a state prefix and often no exception or status, so the monitor title names the pattern."""
    body = DD_STATE.sub("", text.strip())
    svc = (SERVICE.search(body) or [None, "?"])[1]
    r = ROUTE.search(body)
    route = f"{r.group(1)} {UUID.sub('{id}', r.group(2))}" if r else "-"
    err = (EXC.search(body) or [None, None])[1] or (STATUS.search(body) or [None, None])[1]
    if not err:
        tm = TITLE.search(body)
        err = re.sub(r"\d+", "N", tm.group(1).strip())[:60] if tm else "unknown"
    return f"{svc}|{route}|{err}"


def _ledger() -> dict:
    return db.kv_get("alert_patterns", {})


def record(ev: dict) -> dict | None:
    """Update the ledger for one alert event; return an escalation dict when one is due."""
    text = ev["text"] or ""
    fp = fingerprint(text)
    led = _ledger()
    if re.match(r"^\W*(recovered|recuperado)\b", text.strip(), re.I) or "Recuperado:" in text[:200]:
        # a recovery is not a hit: it ends the episode
        if fp in led:
            led[fp]["recovered_at"] = now()
            db.kv_set("alert_patterns", led)
        return None
    e = led.setdefault(fp, {"count": 0, "hits": [], "first_seen": now(), "status": "watched",
                            "in_fence": site.in_fence(text, "alert_re"), "sample": text[:600]})
    e["count"] += 1
    e["hits"] = [h for h in e["hits"] if h > now() - 7 * 86400] + [now()]
    e["last_seen"] = now()
    e["last_event"] = ev["id"]
    h24 = sum(1 for h in e["hits"] if h > now() - 86400)
    h7 = len(e["hits"])
    esc = None
    if e["status"] == "watched":
        if e["count"] == 1 and e["in_fence"] and BODY.search(text):
            esc = {"why": "first sight, in fence, real error body", "fp": fp}
            e["status"] = "looked"
        elif h24 >= 3 or h7 >= 10:
            esc = {"why": f"{h24} hits in 24h / {h7} in 7d", "fp": fp}
            e["status"] = "surfaced"
            e["surfaced_count"] = e["count"]
    elif e["status"] == "looked" and (h24 >= 3 or h7 >= 10):
        esc = {"why": f"recurring after the first look: {h24} in 24h / {h7} in 7d", "fp": fp}
        e["status"] = "surfaced"
        e["surfaced_count"] = e["count"]
    elif e["status"] == "surfaced":
        quiet = e.get("quiet_until") and now() > e["quiet_until"]
        if e["count"] >= 3 * e.get("surfaced_count", 1) or quiet:
            esc = {"why": "tripled since surfacing" if not quiet else "came back after 3 quiet days", "fp": fp}
            e["surfaced_count"] = e["count"]
    elif e["status"] == "mitigated" and now() - e.get("mitigated_at", now()) > 48 * 3600 and h24 >= 3:
        esc = {"why": "still failing 48h after the mitigation merged", "fp": fp}
        e["status"] = "surfaced"
        e["surfaced_count"] = e["count"]
    db.kv_set("alert_patterns", led)
    return esc


def escalate(ev: dict, esc: dict) -> int | None:
    """One INCIDENT per fingerprint while it is open; dedupe by thread key."""
    fp = esc["fp"]
    tk = f"alert:{fp}"
    if db.open_item_for_thread(tk):
        return None
    e = _ledger()[fp]
    h24 = sum(1 for h in e["hits"] if h > now() - 86400)
    since = db.fmt_time(e['first_seen'])
    summary = site.t(f"Alert pattern {fp}: {e['count']} in total, {h24} in 24h, {len(e['hits'])} in 7 days, "
                     f"seen since {since}. Reason: {esc['why']}.",
                     f"Patrón de alerta {fp}: {e['count']} en total, {h24} en 24h, {len(e['hits'])} en 7 días, "
                     f"visto desde {since}. Motivo: {esc['why']}.")
    iid = db.create_item("INCIDENT", "telemetry", site.t(f"alert {fp}", f"alerta {fp}"), tk, urgency=3 if e["in_fence"] else 2,
                         data={"channel": ev["channel"], "ts": ev["ts"], "thread_ts": ev["thread_ts"] or ev["ts"],
                               "text": ev["text"][:4000], "author": ev["user"], "summary": summary,
                               "pattern": fp, "pattern_stats": {"count": e["count"], "h24": h24, "h7": len(e["hits"]),
                                                                "first_seen": e["first_seen"], "in_fence": e["in_fence"]},
                               "telemetry_done": False})
    db.x("UPDATE events SET item_id=? WHERE id=?", iid, ev["id"])
    log("alerts: escalated pattern", fp=fp, why=esc["why"], item=iid)
    return iid


def mitigated(fp: str) -> None:
    led = _ledger()
    if fp in led:
        led[fp]["status"] = "mitigated"
        led[fp]["mitigated_at"] = now()
        db.kv_set("alert_patterns", led)


def ingest(ev: dict) -> bool:
    """Called for every alerts-channel event instead of model triage. Returns True when handled."""
    esc = record(ev)
    if esc:
        escalate(ev, esc)
    db.x("UPDATE events SET triaged=1 WHERE id=?", ev["id"])
    return True

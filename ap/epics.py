"""Epics: the user's open PRs grouped by the thing they deliver together.
Deterministic: groups come from merge-order dependencies plus shared branch/title tokens; the
verdict comes from deploy semantics (schools/api-cometa: dev on merge, prod on release; cometa
frontend: prod on merge) and from what else is still open on the same topic."""
from __future__ import annotations

import json
import re

from . import db, gh, merge_order, site
from .core import CFG

STOP = {"feat", "fix", "test", "chore"}


def _names() -> list[tuple[str, str]]:
    """token regex -> epic name, first match wins (config epics.names). None named: PRs group by dependencies and
    shared words, and an epic is called after its first PR."""
    return list(((CFG.get("epics") or {}).get("names") or {}).items())


def _tokens(p: dict) -> set[str]:
    words = re.split(r"[^a-z0-9]+", f"{p.get('headRefName', '')} {p.get('title', '')}".lower())
    stop = STOP | site.stop_words("epics")
    return {w for w in words if len(w) >= 5 and w not in stop}


def _epic_of(p: dict) -> str | None:
    blob = f"{p.get('headRefName', '')} {p.get('title', '')}".lower()
    return next((name for rx, name in _names() if re.search(rx, blob)), None)


def _name(prs: list[dict]) -> str:
    blob = " ".join(f"{p.get('headRefName', '')} {p.get('title', '')}" for p in prs).lower()
    for rx, name in _names():
        if re.search(rx, blob):
            return name
    t = prs[0].get("title", "")
    return re.sub(r"^\w+\([^)]*\):\s*", "", t)[:50]


def groups(own: dict[tuple[str, int], dict]) -> list[dict]:
    """[{name, prs: [(repo, n, pr)] ordered backend-first}] over open own PRs."""
    keys = list(own)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k

    def union(a, b):
        parent[find(a)] = find(b)

    open_set = set(keys)
    for d in db.q("SELECT repo, number, dep_repo, dep_number FROM merge_deps"):
        a, b = (d["repo"], d["number"]), (d["dep_repo"], d["dep_number"])
        if a in open_set and b in open_set:
            union(a, b)
    toks = {k: _tokens(own[k]) for k in keys}
    named = {k: _epic_of(own[k]) for k in keys}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if named[a] and named[a] == named[b]:
                union(a, b)
            elif not named[a] and not named[b] and len(toks[a] & toks[b]) >= 2:
                union(a, b)
    by_root: dict = {}
    for k in keys:
        by_root.setdefault(find(k), []).append(k)
    out = []
    for members in by_root.values():
        # backend (schools/api-cometa) first, then frontend; deps decide ties
        members = sorted(members, key=lambda k: (1 if site.has_layer(k[0], "frontend") else 0, k[1]))
        out.append({"name": _name([own[k] for k in members]), "prs": [(k[0], k[1], own[k]) for k in members]})
    out.sort(key=lambda g: (-len(g["prs"]), g["name"]))
    return out


def _reviews(repo: str, n: int) -> list[dict] | None:
    """The PR's reviews, or the last ones read when GitHub does not answer (None: never read). 10-11: gh answered 404
    on one of the 18 reviews reads in most board renders, so render raised, digest fell back to the flat 'Tus PRs'
    layout and the next clean render went back to epics: each flip posted or deleted a board reply and edited the
    other 17, 101 self-DM writes in 6 h. A failed read is no news, the layout stays."""
    key = f"epic_reviews:{repo}#{n}"
    try:
        revs = [{"user": {"login": (r.get("user") or {}).get("login", "")}, "state": r.get("state")} for r in gh.reviews(repo, n)]
    except Exception:
        return db.kv_get(key)
    db.kv_set(key, revs)
    return revs


def pr_state(repo: str, n: int, pr: dict, st: dict) -> str:
    s = st.get("summary") or {}
    threads = st.get("unresolved_threads") or 0
    revs = _reviews(repo, n)
    humans = [(r.get("user") or {}).get("login") for r in revs or []
              if r.get("state") == "APPROVED" and not re.search(r"bot|coderabbit", (r.get("user") or {}).get("login", ""), re.I)
              and (r.get("user") or {}).get("login") != CFG["self_github"]]
    changes = [(r.get("user") or {}).get("login") for r in revs or []
               if r.get("state") == "CHANGES_REQUESTED" and not re.search(r"bot|coderabbit", (r.get("user") or {}).get("login", ""), re.I)]
    bits = []
    if pr.get("isDraft"):
        bits.append(site.t("draft", "borrador"))
    waiting = db.q1("SELECT json_extract(data,'$.waiting_for') w FROM items WHERE repo=? AND pr=? AND json_extract(data,'$.waiting_for') IS NOT NULL "
                    "AND status NOT IN ('done','dropped','expired','failed') ORDER BY updated_at DESC", repo, n)
    dep = re.search(r"[a-z-]+#\d+", (waiting["w"] if waiting and waiting["w"] else "") or "")
    if waiting and waiting["w"] and waiting["w"].startswith("waits_for_deploy"):
        what = dep.group(0) if dep else site.t("its backend", "su backend")
        bits.append(site.t(f"CI red by design until {what} is on dev", f"CI rojo por diseño hasta que {what} esté en dev"))
    elif s.get("conflicting"):
        base = s.get('base') or 'main'
        bits.append(site.t(f"conflicts with {base}", f"conflictos con {base}")
                    + (site.t(" (I am updating it)", " (la estoy actualizando)") if not st.get("blocked") else ""))
    elif s.get("green"):
        bits.append(site.t("CI green", "CI verde"))
    elif s.get("failed"):
        bits.append(site.t("CI red", "CI rojo"))
    else:
        bits.append(site.t("CI running", "CI corriendo"))
    if st.get("blocked"):
        # babysit's block reads "<what>, pero/but <who is on it>:...", in the install's language
        bits.append(site.t("not touching it: ", "no la toco: ") + re.split(r", (?:pero|but) ", st["blocked"], maxsplit=1)[-1].split(":")[0])
    if changes:
        bits.append(site.t("changes requested by ", "cambios pedidos por ") + ", ".join(changes))
    elif humans:
        bits.append(site.t("approved by ", "aprobado por ") + ", ".join(humans))
    elif revs is not None:
        bits.append(site.t("needs a human approval", "falta approve humano"))
    if threads:
        bits.append(site.t(f"{threads} unresolved CodeRabbit thread{'s' if threads != 1 else ''} (I am answering them)",
                           f"{threads} hilo{'s' if threads != 1 else ''} de CodeRabbit sin resolver (lo estoy respondiendo)"))
    return ", ".join(bits)


def verdict(group: dict, own: dict) -> str:
    """One line: does merging these PRs complete the epic in prod, in what order, and what else is open."""
    prs = group["prs"]
    backend = [(r, n) for r, n, _ in prs if not site.has_layer(r, "frontend")]
    front = [(r, n) for r, n, _ in prs if site.has_layer(r, "frontend")]
    toks = set().union(*(_tokens(p) for _, _, p in prs))
    other_work = [r["title"] for r in db.q(
        "SELECT title FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('done','dropped','expired','failed','awaiting_approval','awaiting_human')")
        if len(toks & set(re.split(r"[^a-z0-9]+", r["title"].lower()))) >= 2]
    migr = [f"{r}#{n}" for r, n, p in prs if merge_order.has_migration(r, [f["path"] for f in (p.get("files") or [])])]
    parts = []
    if not other_work:
        parts.append(site.t(f"complete with {'this PR' if len(prs) == 1 else f'these {len(prs)} PRs'}: no other open pieces",
                            f"completo con {'este PR' if len(prs) == 1 else f'estos {len(prs)} PRs'}: no hay otras piezas abiertas"))
    else:
        parts.append(site.t(f"there are also {len(other_work)} task(s) in progress on this topic: ",
                            f"además hay {len(other_work)} tarea(s) en curso del tema: ") + "; ".join(t[:50] for t in other_work[:2]))
    order = []
    back_refs, front_refs = ', '.join(f'{r}#{n}' for r, n in backend), ', '.join(f'{r}#{n}' for r, n in front)
    if backend:
        order.append(site.t(f"1) merge {back_refs} (it goes to dev on its own; to prod with the next release)",
                            f"1) mergear {back_refs} (va a dev solo; a prod con el próximo release)"))
    if front:
        order.append((site.t(f"{len(order) + 1}) once {back_refs} is in prod, merge ", f"{len(order) + 1}) cuando {back_refs} esté en prod, mergear ")
                      if backend else site.t("merge ", "mergear "))
                     + site.t(f"{front_refs} (the frontend goes to prod on merge)", f"{front_refs} (el front sale a prod al mergear)"))
    if order:
        parts.append(site.t("order: ", "orden: ") + "; ".join(order))
    if migr:
        migr_ = ', '.join(migr)
        parts.append(site.t(f"migrations in {migr_} (they run on their own during the deploy)", f"migraciones en {migr_} (corren solas en el deploy)"))
    return ". ".join(parts)


def render(own: dict[tuple[str, int], dict], states: dict[tuple[str, int], dict]) -> list[str]:
    lines = []
    for g in groups(own):
        lines.append(f"*{g['name']}* · {len(g['prs'])} PR{'s' if len(g['prs']) != 1 else ''}")
        for r, n, p in g["prs"]:
            role = "front" if site.has_layer(r, "frontend") else "backend"
            lines.append(f"   • <{site.pr_url(r, n)}|{r}#{n}> ({role}): {pr_state(r, n, p, states.get((r, n), {}))}")
        lines.append(f"   → {verdict(g, own)}")
    return lines

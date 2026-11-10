"""Langfuse tracing (v1 had it; v2 lost it). Self-hosted Langfuse at localhost:3663, keys in
the keychain (cometa-autopilot-langfuse-pk/sk). Two paths, both best effort, never blocking:
- Claude CLI calls export OpenTelemetry traces themselves when the OTEL env is set (per-request
  tokens, tool calls), exactly as v1's run-tick.sh did;
- free-model (opencode) calls and stage outcomes are posted as Langfuse traces through the
  public ingestion API from a background thread.
If Langfuse is down, everything runs untraced and a health probe retries every 5 minutes."""
from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
import urllib.request
import uuid

from . import site
from .core import CFG, log

URL = CFG.get("langfuse", {}).get("url", "http://localhost:3663")
_state = {"keys": None, "ok_until": 0.0, "checked": 0.0}
_lock = threading.Lock()


def _keys() -> tuple[str, str] | None:
    if _state["keys"] is None:
        try:
            pk = subprocess.run(["security", "find-generic-password", "-s", site.keychain("langfuse_pk"), "-w"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
            sk = subprocess.run(["security", "find-generic-password", "-s", site.keychain("langfuse_sk"), "-w"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
            _state["keys"] = (pk, sk) if pk and sk else ()
        except Exception:
            _state["keys"] = ()
    return _state["keys"] or None


def healthy() -> bool:
    with _lock:
        if time.time() < _state["ok_until"]:
            return True
        if time.time() - _state["checked"] < 300:
            return False
        _state["checked"] = time.time()
    try:
        with urllib.request.urlopen(f"{URL}/api/public/health", timeout=3) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    if ok:
        _state["ok_until"] = time.time() + 300
    return ok


def _auth() -> str:
    pk, sk = _keys()
    return "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()


def otel_env(service: str) -> dict:
    """Env for a Claude CLI call so it exports its own OTel traces to Langfuse."""
    if not _keys() or not healthy():
        return {}
    return {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_TRACES_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"{URL}/api/public/otel",
            "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization={_auth()}",
            "OTEL_TRACES_EXPORT_INTERVAL": "1000", "OTEL_SERVICE_NAME": f"autopilot2-{service}",
            "OTEL_RESOURCE_ATTRIBUTES": f"tier=claude,lane={service}"}


def record(tier: str, lane: str, *, item_id: int | None, ok: bool, dur_s: float, model: str = "",
           in_tok: int = 0, out_tok: int = 0, usd: float = 0.0, note: str = "", prompt: str = "", output: str = "") -> None:
    """One Langfuse trace + generation for a model call. Fire and forget."""
    if not _keys() or not healthy():
        return
    tid = str(uuid.uuid4())
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    start_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - dur_s))
    batch = [
        {"id": str(uuid.uuid4()), "type": "trace-create", "timestamp": now_iso, "body": {
            "id": tid, "name": f"{tier}/{lane}", "sessionId": f"item-{item_id}" if item_id else None,
            "tags": [tier, lane.split("-")[0]] + (["ok"] if ok else ["failed"]),
            "metadata": {"item": item_id, "note": note[:300]}, "input": prompt[:4000] or None, "output": output[:4000] or None}},
        {"id": str(uuid.uuid4()), "type": "generation-create", "timestamp": now_iso, "body": {
            "id": str(uuid.uuid4()), "traceId": tid, "name": lane, "model": model or tier, "startTime": start_iso, "endTime": now_iso,
            "usage": {"input": in_tok, "output": out_tok, "totalCost": usd}, "level": "DEFAULT" if ok else "ERROR",
            "statusMessage": note[:300], "metadata": {"item": item_id}}},
    ]
    req = urllib.request.Request(f"{URL}/api/public/ingestion", data=json.dumps({"batch": batch}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": _auth()}, method="POST")

    def send():
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log("langfuse ingest failed", err=str(e)[:120])
    threading.Thread(target=send, name="langfuse", daemon=True).start()

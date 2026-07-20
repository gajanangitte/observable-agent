"""SigNoz-triggered heal bridge: the alert IS the trigger.

A long-running service that turns a real SigNoz alert into an autonomous,
policy-gated heal, then watches SigNoz declare the incident resolved. It learns
an alert is FIRING two ways, and either path starts the same governed heal:

  * poll  -- GET /api/v1/rules and watch each rule's ``state`` flip to "firing".
             Only needs outbound Windows -> SigNoz (which always works here), so
             it is the robust default and also how it confirms the resolve.
  * push  -- POST /alert, an Alertmanager-style webhook SigNoz can call directly
             when its notification channel can reach this host.

On a firing alert the bridge opens a ``heal.trigger`` span and injects that trace
context into the heal subprocess, so the alert handoff and the whole
``agent.heal`` loop are ONE distributed trace in SigNoz. Then it launches the
governed heal (``self_heal.py --no-seed``) and polls the same rule until it
returns to resolved, recording how long the loop took to close.

No new dependencies: the standard-library http.server + urllib, plus the
project's existing OpenTelemetry setup.
"""
import os

# Emit the bridge's spans under the healer service, exactly like self_heal.py,
# and BEFORE importing config/telemetry (config reads OTEL_SERVICE_NAME on import).
os.environ.setdefault("OTEL_SERVICE_NAME", "self-healer")

import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import (Link, SpanContext, SpanKind, Status, StatusCode,
                                 TraceFlags)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import telemetry

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNOZ_BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080").rstrip("/")
API_KEY = ""
_KEYFILE = Path(HERE) / ".signoz_api_key"
if _KEYFILE.exists():
    API_KEY = _KEYFILE.read_text().strip()

POLL_S = float(os.getenv("HEAL_BRIDGE_POLL_S", "8"))
LISTEN_HOST = os.getenv("HEAL_BRIDGE_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("HEAL_BRIDGE_PORT", "8099"))
RESOLVE_TIMEOUT_S = float(os.getenv("HEAL_BRIDGE_RESOLVE_TIMEOUT_S", "600"))
COOLDOWN_S = float(os.getenv("HEAL_BRIDGE_COOLDOWN_S", "45"))
HEAL_MODEL = os.getenv("HEAL_MODEL", "qwen2.5:3b")

FIRING_STATES = ("firing", "pending")
RESOLVED_STATES = ("inactive", "normal", "resolved", "ok", "disabled")

tracer = trace.get_tracer("self-healer")

_lock = threading.Lock()
_busy = False
# Cooldown is keyed per alert so a just-healed retry alert does not muzzle a
# distinct cost alert; a single global _busy still serialises the actual heals.
_cooldown_until = {}
_INCIDENT_TRACE_FILE = Path(HERE) / ".last_incident_trace"
_INCIDENT_MAX_AGE_S = float(os.getenv("HEAL_INCIDENT_MAX_AGE_S", "1800"))


def _alert_key(alert_id, alert_name):
    return str(alert_id) if alert_id else (alert_name or "unknown")


def _incident_link():
    """Build an OTel Link from the heal to the app trace that triggered it, using
    the trace id the breaching workload dropped in .last_incident_trace. Returns
    (Link | None, trace_id_hex | None). Best-effort: never fatal."""
    try:
        if not _INCIDENT_TRACE_FILE.exists():
            return None, None
        d = json.loads(_INCIDENT_TRACE_FILE.read_text())
        if time.time() - float(d.get("ts", 0)) > _INCIDENT_MAX_AGE_S:
            return None, None                      # stale: unrelated older incident
        tid = int(d["trace_id"], 16)
        sid = int(d["span_id"], 16)
        if not tid or not sid:
            return None, None
        ctx = SpanContext(trace_id=tid, span_id=sid, is_remote=True,
                          trace_flags=TraceFlags(TraceFlags.SAMPLED))
        return (Link(ctx, {"link.kind": "triggering_incident",
                           "service.name": d.get("service", "observable-agent")}),
                d["trace_id"])
    except Exception:  # noqa: BLE001
        return None, None


def _scenario_for(alert_name):
    """Map an alert to the incident the healer should chase, or None if the alert
    is notify-only and must NOT trigger a heal (e.g. the healer's own backstop --
    letting a failed heal launch another heal would be a feedback loop)."""
    n = (alert_name or "").lower()
    if "self-healer" in n or "backstop" in n or "auto-resolved" in n:
        return None
    if "cost" in n or "spend" in n or "bill" in n or "budget" in n:
        return "cost"
    return "retry"


def _api(path):
    req = urllib.request.Request(SIGNOZ_BASE + path, headers={"SIGNOZ-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _rules():
    d = _api("/api/v1/rules")
    data = d.get("data", d)
    rules = data.get("rules", data) if isinstance(data, dict) else data
    return rules if isinstance(rules, list) else []


def _current_state(alert_id, alert_name):
    """The live state of the triggering rule, matched by id first, then by name."""
    try:
        rules = _rules()
    except Exception:
        return None
    if alert_id is not None:
        for r in rules:
            if str(r.get("id")) == str(alert_id):
                return (r.get("state") or "").lower()
    if alert_name:
        for r in rules:
            nm = r.get("alert") or r.get("alertName") or ""
            if nm and alert_name.lower() in nm.lower():
                return (r.get("state") or "").lower()
    return None


def _run_heal(scenario, alert_name, carrier):
    """Launch the governed heal as a subprocess, sharing the trigger's trace."""
    env = dict(os.environ)
    env["OTEL_SERVICE_NAME"] = "self-healer"
    env["PYTHONUTF8"] = "1"
    env["AGENT_MODEL"] = HEAL_MODEL
    if carrier.get("traceparent"):
        env["TRACEPARENT"] = carrier["traceparent"]
    cmd = [sys.executable, os.path.join(HERE, "self_heal.py"),
           "--scenario", scenario, "--no-seed", "--triggered-by", str(alert_name)]
    print(f"[BRIDGE] launching governed heal: {' '.join(cmd[1:])}", flush=True)
    return subprocess.run(cmd, cwd=HERE, env=env).returncode


def handle_firing(alert_id, alert_name, source):
    """Run one alert-triggered heal episode, then watch the alert resolve."""
    global _busy
    scenario = _scenario_for(alert_name)
    if scenario is None:
        # A notify-only alert (e.g. the healer's own backstop) is never a trigger.
        return
    key = _alert_key(alert_id, alert_name)
    with _lock:
        if _busy or time.time() < _cooldown_until.get(key, 0.0):
            return
        _busy = True
    print(f"\n[BRIDGE] ALERT FIRING ({source}): {alert_name!r} -> heal scenario '{scenario}'",
          flush=True)
    try:
        link, incident_tid = _incident_link()
        links = [link] if link else []
        with tracer.start_as_current_span("heal.trigger", kind=SpanKind.CONSUMER,
                                          links=links) as span:
            span.set_attribute("alert.id", str(alert_id))
            span.set_attribute("alert.name", str(alert_name))
            span.set_attribute("alert.source", source)
            span.set_attribute("heal.scenario", scenario)
            if incident_tid:
                # The app trace that tripped the SLO -> one click from heal to cause.
                span.set_attribute("heal.incident.trace_id", incident_tid)
                print(f"[BRIDGE] linked heal to triggering incident trace {incident_tid}",
                      flush=True)
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)  # -> the heal subprocess
            t0 = time.time()
            rc = _run_heal(scenario, alert_name, carrier)
            span.set_attribute("heal.subprocess.rc", rc)

            print(f"[BRIDGE] heal exited rc={rc}; watching '{alert_name}' for resolve "
                  f"(SigNoz closes the loop)...", flush=True)
            resolved = False
            deadline = time.time() + RESOLVE_TIMEOUT_S
            while time.time() < deadline:
                st = _current_state(alert_id, alert_name)
                if st in RESOLVED_STATES:
                    resolved = True
                    break
                time.sleep(POLL_S)
            secs = time.time() - t0
            span.set_attribute("alert.resolved", resolved)
            span.set_attribute("loop.close_seconds", round(secs, 1))
            if resolved:
                print(f"[BRIDGE] alert RESOLVED after {secs:.0f}s -- SigNoz closed the loop.",
                      flush=True)
                span.set_status(Status(StatusCode.OK))
            else:
                print(f"[BRIDGE] alert still firing after {secs:.0f}s (resolve timeout).",
                      flush=True)
                span.set_status(Status(StatusCode.ERROR, "alert not resolved in time"))
    finally:
        with _lock:
            _busy = False
            _cooldown_until[key] = time.time() + COOLDOWN_S


def trigger_async(alert_id, alert_name, source):
    """Start a heal episode in the background unless one is already in flight, or
    the alert is notify-only (no mapped scenario)."""
    if _scenario_for(alert_name) is None:
        return False
    key = _alert_key(alert_id, alert_name)
    with _lock:
        if _busy or time.time() < _cooldown_until.get(key, 0.0):
            return False
    threading.Thread(target=handle_firing, args=(alert_id, alert_name, source),
                     daemon=True).start()
    return True


def poll_loop():
    """Watch every SigNoz rule's state; trigger a heal on the transition to firing."""
    prev = {}
    print(f"[BRIDGE] polling {SIGNOZ_BASE}/api/v1/rules every {POLL_S:.0f}s "
          f"({'api key loaded' if API_KEY else 'NO API KEY'})", flush=True)
    while True:
        try:
            for r in _rules():
                rid = str(r.get("id"))
                name = r.get("alert") or r.get("alertName") or rid
                state = (r.get("state") or "").lower()
                was = prev.get(rid)
                prev[rid] = state
                if state in FIRING_STATES and was not in FIRING_STATES:
                    print(f"[BRIDGE] rule {name!r} state {was} -> {state}", flush=True)
                    trigger_async(rid, name, "poll")
        except Exception as e:  # noqa: BLE001
            print("[BRIDGE] poll error:", e, flush=True)
        time.sleep(POLL_S)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self._send(200, {"ok": True, "busy": _busy})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/alert", "/webhook"):
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        triggered = False
        alerts = payload.get("alerts") or []
        if alerts:
            for a in alerts:
                st = (a.get("status") or payload.get("status") or "firing").lower()
                if st != "firing":
                    continue
                labels = a.get("labels") or {}
                name = labels.get("alertname") or payload.get("alertname") or "SigNoz alert"
                aid = labels.get("ruleId") or labels.get("id") or None
                triggered = trigger_async(aid, name, "webhook") or triggered
        else:
            st = (payload.get("status") or "firing").lower()
            if st == "firing":
                name = payload.get("alertname") or "SigNoz alert"
                triggered = trigger_async(None, name, "webhook")
        self._send(202, {"accepted": True, "triggered": triggered})

    def log_message(self, *_):  # silence per-request logging
        pass


def main():
    telemetry.setup_telemetry()
    print("[BRIDGE] SigNoz alert -> governed heal bridge", flush=True)
    print(f"[BRIDGE] webhook: POST http://{LISTEN_HOST}:{LISTEN_PORT}/alert", flush=True)
    print(f"[BRIDGE] heal model: {HEAL_MODEL}", flush=True)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\n[BRIDGE] shutting down.", flush=True)
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()

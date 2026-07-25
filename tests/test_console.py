"""Unit tests for console.py (the dependency-free read-only status console).

Network-free and file-isolated: the pure snapshot/render layer is tested by
pointing the module's readers at a temp directory, so no real state file is
touched and no socket is opened. Confirms the console renders on a fresh clone
(all sections empty), reflects real state, HTML-escapes untrusted values, and that
the JSON API body round-trips.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console
import heal_ledger


def _isolate():
    """Redirect console + ledger at a fresh temp dir; return (dir, undo)."""
    d = tempfile.mkdtemp()
    saved_here = console.HERE
    saved_led = heal_ledger.PATH
    console.HERE = d
    heal_ledger.PATH = os.path.join(d, "heal_ledger.json")

    def undo():
        console.HERE = saved_here
        heal_ledger.PATH = saved_led
    return d, undo


def _write(d, name, obj):
    with open(os.path.join(d, name), "w") as f:
        json.dump(obj, f)


def test_snapshot_on_fresh_clone():
    d, undo = _isolate()
    try:
        s = console.snapshot()
        assert s["memory"] == []
        assert s["chaos_armed"] == []
        assert s["ledger"]["intact"] is True
        assert s["ledger"]["records"] == 0
        assert s["mcp2"] is None
        assert "service.version" in s["version"]
        assert "response_drop" in s["faults_catalog"]
    finally:
        undo()


def test_snapshot_reflects_state():
    d, undo = _isolate()
    try:
        _write(d, "heal_state.json",
               {"chaos_drop": True, "mitigation": False, "runaway": False,
                "model": "llama3.2:1b", "cost_budget_usd": 0.0})
        _write(d, "heal_memory.json", {
            "abc:enable_mitigation": {
                "class_id": "abc", "slo": "retry_tax", "action_base": "enable_mitigation",
                "count": 4, "proven_severity": "high", "mttr_ms_best": 41000,
                "trace_id": "deadbeefcafe0000", "verified": True}})
        heal_ledger.append("breach.detected", {"slo": "retry_tax"}, path=heal_ledger.PATH)
        heal_ledger.append("outcome", {"healed": True}, path=heal_ledger.PATH)

        s = console.snapshot()
        # response_drop is armed (chaos_drop true + mitigation false)
        assert "response_drop" in s["chaos_armed"]
        assert len(s["memory"]) == 1
        assert s["memory"][0]["action"] == "enable_mitigation"
        assert s["memory"][0]["proven"] == 4
        assert s["ledger"]["records"] == 2
        assert s["ledger"]["intact"] is True
        assert s["ledger"]["head_event"] == "outcome"
    finally:
        undo()


def test_snapshot_detects_tampered_ledger():
    d, undo = _isolate()
    try:
        heal_ledger.append("a", {}, path=heal_ledger.PATH)
        heal_ledger.append("b", {}, path=heal_ledger.PATH)
        chain = json.load(open(heal_ledger.PATH))
        chain[0]["data"]["x"] = "tampered"
        json.dump(chain, open(heal_ledger.PATH, "w"))
        s = console.snapshot()
        assert s["ledger"]["intact"] is False
    finally:
        undo()


def test_render_html_is_bytes_and_complete():
    d, undo = _isolate()
    try:
        _write(d, "heal_state.json", {"chaos_drop": False, "runaway": False,
                                       "mitigation": False, "model": "llama3.2:1b",
                                       "cost_budget_usd": 0.0})
        body = console.render_html()
        assert isinstance(body, bytes)
        text = body.decode("utf-8")
        assert "<!doctype html>" in text
        assert "status console" in text
        assert "Audit ledger" in text
        assert "Verified remediation memory" in text
    finally:
        undo()


def test_render_html_escapes_untrusted_state():
    d, undo = _isolate()
    try:
        # A malicious model name in the state file must be escaped, never injected.
        _write(d, "heal_state.json", {"chaos_drop": False, "runaway": False,
                                       "mitigation": False,
                                       "model": "<script>alert(1)</script>",
                                       "cost_budget_usd": 0.0})
        text = console.render_html().decode("utf-8")
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;" in text
    finally:
        undo()


def test_render_json_roundtrips():
    d, undo = _isolate()
    try:
        body = console.render_json()
        obj = json.loads(body.decode("utf-8"))
        assert "ledger" in obj
        assert "version" in obj
        assert obj["ledger"]["intact"] is True
    finally:
        undo()


def test_snapshot_survives_corrupt_report():
    d, undo = _isolate()
    try:
        # A truncated/garbage report file must not crash the console.
        with open(os.path.join(d, "mcp2_report.json"), "w") as f:
            f.write("{not valid json")
        s = console.snapshot()
        assert s["mcp2"] is None
    finally:
        undo()


def test_once_cli_renders_without_server():
    # --once must return 0 and not open a socket.
    import contextlib
    import io
    d, undo = _isolate()
    try:
        buf = io.BytesIO()

        class _Wrap:
            buffer = buf
        with contextlib.redirect_stdout(_Wrap()):
            rc = console._main(["--once", "--json"])
        assert rc == 0
        assert json.loads(buf.getvalue().decode("utf-8"))["ledger"]["intact"] is True
    finally:
        undo()

"""Unit tests for the observability plumbing itself, using in-memory OTel exporters
(no OTLP, no network):

  * the incident span-link handoff -- agent._record_incident_trace writes the
    breaching trace id, heal_bridge._incident_link reads it back into a Link, so a
    heal trace is joined to the app trace that triggered it,
  * the trace-correlated structured heal logs -- self_heal._hlog stamps the active
    trace id onto a log record and maps its fields to dotted heal.* attributes with
    their primitive types preserved (the LOGS signal for an incident).

A local TracerProvider gives real, recording spans (non-zero trace ids) without
touching the global provider, so these are deterministic and self-contained.
"""
import json
import logging
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
try:  # non-deprecated name first; fall back for older SDKs
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as _LogExp
except Exception:  # noqa: BLE001
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as _LogExp
from opentelemetry.trace import INVALID_SPAN

import agent
import heal_bridge as B
import self_heal

_tracer = TracerProvider().get_tracer("telemetry-test")


# --- span-link handoff (agent writer <-> bridge reader) ----------------------

def test_record_incident_trace_writes_handoff():
    tmp = os.path.join(tempfile.gettempdir(), "incident_handoff_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    agent._INCIDENT_TRACE_FILE = tmp
    with _tracer.start_as_current_span("agent.invoke") as span:
        tid = span.get_span_context().trace_id
        agent._record_incident_trace(span, "response_dropped")
    d = json.load(open(tmp))
    assert int(d["trace_id"], 16) == tid
    assert d["reason"] == "response_dropped"
    assert "span_id" in d and "ts" in d


def test_incident_writer_reader_roundtrip():
    tmp = os.path.join(tempfile.gettempdir(), "incident_roundtrip_test.json")
    agent._INCIDENT_TRACE_FILE = tmp
    B._INCIDENT_TRACE_FILE = pathlib.Path(tmp)
    with _tracer.start_as_current_span("agent.invoke") as span:
        tid = span.get_span_context().trace_id
        agent._record_incident_trace(span, "response_dropped")
    link, hexid = B._incident_link()
    assert link is not None
    assert int(hexid, 16) == tid
    assert link.context.trace_id == tid


def test_record_incident_trace_noops_on_invalid_span():
    # A non-recording span has trace_id 0 -> the writer must produce no file.
    tmp = os.path.join(tempfile.gettempdir(), "incident_noop_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    agent._INCIDENT_TRACE_FILE = tmp
    agent._record_incident_trace(INVALID_SPAN, "x")
    assert not os.path.exists(tmp)


# --- structured, trace-correlated heal logs (self_heal._hlog) ----------------

def _capture_self_healer_logs():
    lp = LoggerProvider()
    exp = _LogExp()
    lp.add_log_record_processor(SimpleLogRecordProcessor(exp))
    handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
    lg = logging.getLogger("self-healer")
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    return exp, handler, lg


def test_hlog_is_trace_correlated_with_typed_attrs():
    exp, handler, lg = _capture_self_healer_logs()
    try:
        with _tracer.start_as_current_span("agent.heal") as span:
            tid = span.get_span_context().trace_id
            self_heal._hlog("outcome", healed=True, mttr_s=41.5, slo="retry_tax",
                            action="enable_mitigation")
        recs = exp.get_finished_logs()
        assert len(recs) == 1
        lr = recs[0].log_record
        assert lr.trace_id == tid and lr.trace_id != 0     # correlated to the heal trace
        attrs = dict(lr.attributes)
        assert attrs["heal.event"] == "outcome"
        assert attrs["heal.healed"] is True                # bool preserved
        assert attrs["heal.mttr_s"] == 41.5                # float preserved
        assert attrs["heal.slo"] == "retry_tax"
        assert attrs["heal.action"] == "enable_mitigation"
    finally:
        lg.removeHandler(handler)


def test_hlog_prefixes_keys_and_coerces_non_primitives():
    exp, handler, lg = _capture_self_healer_logs()
    try:
        with _tracer.start_as_current_span("agent.heal"):
            self_heal._hlog("decision.recall", **{"heal.source": "memory"},
                            evidence={"k": 1})
        lr = exp.get_finished_logs()[0].log_record
        attrs = dict(lr.attributes)
        assert attrs["heal.event"] == "decision.recall"
        assert attrs["heal.source"] == "memory"            # already prefixed -> kept as-is
        assert attrs["heal.evidence"] == str({"k": 1})     # non-primitive coerced to str
    finally:
        lg.removeHandler(handler)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()

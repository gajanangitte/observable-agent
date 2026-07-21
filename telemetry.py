"""OpenTelemetry wiring: traces, metrics, and logs -> OTLP -> SigNoz.

All three signals are configured explicitly so every agent run produces:
  * a distributed trace  (agent.invoke -> llm.chat -> tool.<name>)
  * custom metrics        (tokens, cost, latency, request counts)
  * trace-correlated logs (each log line carries the active trace_id)
"""
import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import config

_providers = {}
_tokens = _cost = _llm_latency = _tool_latency = _requests = _retries = None
_cost_break = None


def _tag(attrs):
    """Tag metric attributes with the active experiment id when one is set, so
    control vs chaos cohorts are directly comparable in SigNoz."""
    if config.EXPERIMENT_ID:
        return {**attrs, "experiment.id": config.EXPERIMENT_ID}
    return attrs


def setup_telemetry():
    resource = Resource.create({
        "service.name": config.SERVICE_NAME,
        "service.version": "1.0.0",
        "deployment.environment": config.ENVIRONMENT,
    })
    base = config.OTLP_ENDPOINT.rstrip("/")
    # Fail fast: under heavy local CPU load the SigNoz ingester can be slow to
    # answer, so keep per-attempt timeouts short and let the batch processors
    # retry rather than blocking the agent.
    timeout_s = config.OTLP_TIMEOUT_S

    # ---- Traces ----
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=timeout_s)))
    trace.set_tracer_provider(tp)

    # ---- Metrics ----
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=timeout_s),
        export_interval_millis=5000,
    )
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)

    # ---- Logs ----
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs", timeout=timeout_s)))
    set_logger_provider(lp)
    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(otel_handler)
    root.addHandler(logging.StreamHandler())  # also print to console

    # Quiet noisy third-party loggers. This also avoids a feedback loop where
    # OTLP export-failure logs would themselves be captured and re-exported.
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    otel_log = logging.getLogger("opentelemetry")
    otel_log.setLevel(logging.ERROR)
    otel_log.propagate = False

    # ---- Metric instruments ----
    global _tokens, _cost, _llm_latency, _tool_latency, _requests, _retries, _cost_break
    meter = metrics.get_meter("observable-agent")
    _tokens = meter.create_counter(
        "gen_ai.client.token.usage", unit="{token}",
        description="LLM tokens consumed by the agent")
    _cost = meter.create_counter(
        "gen_ai.client.cost", unit="USD",
        description="Estimated LLM spend")
    _llm_latency = meter.create_histogram(
        "gen_ai.client.operation.duration", unit="ms",
        description="LLM call latency")
    _tool_latency = meter.create_histogram(
        "agent.tool.duration", unit="ms",
        description="Agent tool execution latency")
    _requests = meter.create_counter(
        "agent.requests", unit="{request}",
        description="Agent requests handled")
    _retries = meter.create_counter(
        "agent.retry.count", unit="{retry}",
        description="LLM retries triggered (e.g. after a dropped response)")
    _cost_break = meter.create_counter(
        "agent.cost.circuit_break", unit="{event}",
        description="Times the per-request cost budget severed further LLM calls")

    _providers.update(trace=tp, metrics=mp, logs=lp)
    return tp, mp, lp


def record_llm(model, input_tokens, output_tokens, latency_ms, status="ok"):
    cost = config.cost_usd(model, input_tokens, output_tokens)
    attrs = _tag({"gen_ai.request.model": model, "gen_ai.system": "ollama", "status": status})
    _tokens.add(input_tokens, {**attrs, "gen_ai.token.type": "input"})
    _tokens.add(output_tokens, {**attrs, "gen_ai.token.type": "output"})
    _cost.add(cost, attrs)
    _llm_latency.record(latency_ms, attrs)
    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        span.set_attribute("gen_ai.usage.total_tokens", input_tokens + output_tokens)
        span.set_attribute("gen_ai.usage.cost_usd", round(cost, 6))
    # WattTrace GreenOps live hook: attach an energy/carbon estimate to this same
    # call. Off by default (WATTTRACE_LIVE); fully guarded so it can never break
    # a real inference path.
    if os.getenv("WATTTRACE_LIVE"):
        try:
            import watt_metrics
            watt_metrics.on_llm(model, input_tokens, output_tokens, latency_ms, status)
        except Exception:
            pass
    return cost


def record_tool(name, latency_ms, status="ok"):
    _tool_latency.record(latency_ms, _tag({"tool.name": name, "status": status}))


def record_request(status="ok"):
    _requests.add(1, _tag({"status": status}))


def record_retry(model, reason):
    """Count one retry (e.g. a dropped response forced the agent to re-infer)."""
    _retries.add(1, _tag({"gen_ai.request.model": model, "retry.reason": reason}))


def record_cost_break(model, spent_usd, budget_usd):
    """Count one cost circuit-break: the per-request budget severed further calls."""
    _cost_break.add(1, _tag({"gen_ai.request.model": model,
                             "cost.budget_usd": round(budget_usd, 6)}))


def shutdown():
    """Flush and close all exporters so nothing is lost on exit.

    force_flush is bounded so a slow/unreachable collector can never hang the
    agent at exit; anything still buffered is dropped rather than retried."""
    for p in _providers.values():
        try:
            p.force_flush(timeout_millis=config.FLUSH_TIMEOUT_MS)
        except Exception:
            pass
    for p in _providers.values():
        try:
            p.shutdown()
        except Exception:
            pass

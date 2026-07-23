"""A transparent, auto-instrumenting proxy for the MCP streamable-HTTP transport.

Point ANY MCP client at this proxy instead of at the real server, and every
JSON-RPC interaction between them becomes a SigNoz-native CLIENT span
(``mcp.<method>``) and a ``mcp.client.*`` metric, with ZERO code changes to the
client OR the server. The proxy forwards bytes faithfully in both directions
(single-JSON and Server-Sent-Events responses, session headers, notifications,
GET streams and DELETE teardown) and never alters the protocol; instrumentation
is pure observation layered on top.

This is the always-on companion to ``mcp2_cert``: the certification lab actively
probes a server on demand, while the proxy passively lights up whatever real
traffic already flows, using the exact same telemetry vocabulary (so both land on
the one MCP Contract Lab dashboard and its alerts). All parsing and grading lives
in the pure, unit-tested ``mcp2_proxy_core``; this file only moves bytes and opens
spans.

  python mcp2_proxy.py --upstream http://localhost:8000/mcp --port 8009
  #   then point your MCP client at http://localhost:8009/mcp

  python mcp2_proxy.py --no-export            # forward only, no SigNoz export
  set OTEL_SERVICE_NAME=my-proxy              # rename the service (default mcp-proxy)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from urllib.parse import urlparse

# Read at import time by config.SERVICE_NAME, so it must be set BEFORE telemetry
# is imported (same trick mcp2_cert / watt_report use). Override with the env var.
os.environ.setdefault("OTEL_SERVICE_NAME", "mcp-proxy")

from aiohttp import web
import aiohttp

import telemetry
import mcp2_metrics
import mcp2_proxy_core as core

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

tracer = trace.get_tracer("mcp-proxy")

DEFAULT_UPSTREAM = os.getenv("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")
DEFAULT_PORT = int(os.getenv("MCP2_PROXY_PORT", "8009"))
DEFAULT_HOST = os.getenv("MCP2_PROXY_HOST", "127.0.0.1")
# How many response bytes to buffer for grading. The bytes are still streamed to
# the client untouched; this cap only bounds the copy kept for instrumentation so
# a huge or long-lived stream can never grow memory without limit.
INSTRUMENT_CAP = int(os.getenv("MCP2_PROXY_BUFFER_BYTES", str(256 * 1024)))


def _forward_url(upstream: str, request: web.Request) -> str:
    """Forward to the configured upstream endpoint, carrying the query string
    through when the upstream URL does not already pin one."""
    if request.query_string and "?" not in upstream:
        return f"{upstream}?{request.query_string}"
    return upstream


async def _pipe(request: web.Request, up: aiohttp.ClientResponse,
                cap: int) -> "tuple[web.StreamResponse, bytes]":
    """Stream the upstream response straight to the downstream client, keeping a
    bounded copy for grading. Returns the prepared response and the copy."""
    resp = web.StreamResponse(status=up.status,
                              headers=core.filter_response_headers(up.headers))
    await resp.prepare(request)
    buf = bytearray()
    async for chunk in up.content.iter_any():
        await resp.write(chunk)
        if cap and len(buf) < cap:
            buf.extend(chunk[: cap - len(buf)])
    await resp.write_eof()
    return resp, bytes(buf)


def _log(quiet: bool, *parts):
    if not quiet:
        print("  " + "  ".join(str(p) for p in parts), flush=True)


async def _handle_post(request: web.Request) -> web.StreamResponse:
    cfg = request.app["cfg"]
    session: aiohttp.ClientSession = request.app["session"]
    upstream = cfg["upstream"]
    body = await request.read()
    messages, _is_batch = core.parse_messages(body)
    primary = core.primary_message(messages)
    fwd_headers = core.filter_request_headers(request.headers)
    url = _forward_url(upstream, request)

    if primary is None:
        # Not JSON-RPC we understand: forward opaquely, do not fabricate a span.
        try:
            async with session.post(url, data=body, headers=fwd_headers,
                                    allow_redirects=False) as up:
                resp, _ = await _pipe(request, up, 0)
                return resp
        except aiohttp.ClientError as e:
            return web.Response(status=502, text=f"mcp2_proxy upstream error: {e}")

    method = core.request_method(primary)
    tool = core.tool_name(primary) or "*"
    t0 = time.perf_counter()
    span = tracer.start_span(core.span_name(method), kind=SpanKind.CLIENT)
    for k, v in core.request_attrs(primary, upstream, len(body), len(messages)).items():
        span.set_attribute(k, v)

    try:
        async with session.post(url, data=body, headers=fwd_headers,
                                allow_redirects=False) as up:
            resp, sniff = await _pipe(request, up, INSTRUMENT_CAP)
            resp_msgs = core.parse_response_body(
                sniff, up.headers.get("content-type", ""))
            grade = core.classify(resp_msgs, primary, http_ok=True)
    except aiohttp.ClientError as e:
        latency = (time.perf_counter() - t0) * 1000.0
        span.set_attribute("mcp.latency_ms", round(latency, 2))
        span.set_attribute("mcp.ok", False)
        span.set_attribute("mcp.error.class", core.ERR_TRANSPORT)
        span.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"[:200]))
        span.end()
        mcp2_metrics.client_call(tool, method, core.STATUS_FAIL,
                                 core.ERR_TRANSPORT, latency)
        _log(cfg["quiet"], f"{method:16s}", tool, "FAIL(transport)",
             f"{latency:7.1f}ms")
        return web.Response(status=502, text=f"mcp2_proxy upstream error: {e}")

    latency = (time.perf_counter() - t0) * 1000.0
    for k, v in core.response_attrs(grade, latency).items():
        span.set_attribute(k, v)
    if grade["status"] == core.STATUS_FAIL:
        span.set_status(Status(StatusCode.ERROR, "no usable response"))
    span.end()
    mcp2_metrics.client_call(tool, method, grade["status"], grade["error_class"],
                             latency)
    _log(cfg["quiet"], f"{method:16s}", tool, grade["status"], f"{latency:7.1f}ms")
    return resp


async def _handle_passthrough(request: web.Request) -> web.StreamResponse:
    """GET (server-initiated SSE stream) and DELETE (session teardown): forward
    faithfully with a lightweight span, no body grading."""
    cfg = request.app["cfg"]
    session: aiohttp.ClientSession = request.app["session"]
    upstream = cfg["upstream"]
    body = await request.read()
    name = "get_stream" if request.method == "GET" else request.method.lower()
    span = tracer.start_span(f"mcp.{name}", kind=SpanKind.CLIENT)
    span.set_attribute("mcp.transport", "streamable_http")
    span.set_attribute("mcp.method", name)
    span.set_attribute("mcp.server.url", upstream)
    span.set_attribute("mcp2.proxy", True)
    try:
        async with session.request(request.method, _forward_url(upstream, request),
                                   data=body or None,
                                   headers=core.filter_request_headers(request.headers),
                                   allow_redirects=False) as up:
            resp, _ = await _pipe(request, up, 0)
            span.set_attribute("mcp.ok", up.status < 400)
            span.end()
            return resp
    except aiohttp.ClientError as e:
        span.set_attribute("mcp.ok", False)
        span.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"[:200]))
        span.end()
        return web.Response(status=502, text=f"mcp2_proxy upstream error: {e}")


async def _dispatch(request: web.Request) -> web.StreamResponse:
    if request.method == "POST":
        return await _handle_post(request)
    return await _handle_passthrough(request)


def _silence_benign_disconnect(loop, context):
    """A client that closes a keep-alive socket makes the Windows Proactor loop
    raise ConnectionResetError from deep in the transport, unrelated to any request
    we served. Swallow exactly that; defer everything else to the default handler."""
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


def make_app(upstream: str, export: bool, quiet: bool) -> web.Application:
    app = web.Application()
    app["cfg"] = {"upstream": upstream, "export": export, "quiet": quiet}

    async def _on_start(a: web.Application):
        asyncio.get_running_loop().set_exception_handler(_silence_benign_disconnect)
        a["session"] = aiohttp.ClientSession(auto_decompress=False)

    async def _on_clean(a: web.Application):
        await a["session"].close()
        if a["cfg"]["export"]:
            telemetry.shutdown()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_clean)
    app.router.add_route("*", "/{tail:.*}", _dispatch)
    return app


def main():
    ap = argparse.ArgumentParser(
        description="Transparent auto-instrumenting proxy for MCP streamable-HTTP")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                    help=f"real MCP server endpoint (default {DEFAULT_UPSTREAM})")
    ap.add_argument("--host", default=DEFAULT_HOST, help="listen host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port")
    ap.add_argument("--no-export", action="store_true",
                    help="forward only, do not export telemetry to SigNoz")
    ap.add_argument("--quiet", action="store_true", help="do not log each call")
    args = ap.parse_args()

    export = not args.no_export
    if export:
        telemetry.setup_telemetry()
        mcp2_metrics.init()

    path = urlparse(args.upstream).path or "/"
    endpoint = f"http://{args.host}:{args.port}{path}"
    print(f"mcp2_proxy: forwarding {endpoint}  ->  {args.upstream}")
    print(f"  service={os.environ['OTEL_SERVICE_NAME']}  export={export}  "
          f"point your MCP client at {endpoint}")

    app = make_app(args.upstream, export, args.quiet)
    web.run_app(app, host=args.host, port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()

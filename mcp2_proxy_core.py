"""Pure core for the MCP instrumentation proxy (no network, no aiohttp, no OTel).

The proxy (``mcp2_proxy``) sits transparently between ANY MCP client and ANY MCP
server on the streamable-HTTP transport and turns their JSON-RPC traffic into
SigNoz-native spans and ``mcp.client.*`` metrics, with ZERO changes to either
side. This module is the pure half of that: it parses the JSON-RPC envelope of a
request, parses a JSON or Server-Sent-Events response, and grades the outcome into
exactly the same shape the hand-written probe (``mcp2_probe._timed_call``)
produces, so the proxy and the certification lab speak one telemetry vocabulary.

Splitting the pure logic out (exactly like ``mcp2_model`` / ``mcp2_contracts`` sit
under ``mcp2_probe``) means every parsing and grading decision is unit-tested on
hand-built bytes with no server, no browser and no SigNoz. The network shell only
moves bytes and opens spans; it makes no judgements of its own.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

# The status vocabulary is identical to mcp2_probe / mcp2_metrics so proxy traffic
# and cert-lab traffic aggregate together on the same dashboards and alerts.
STATUS_OK = "ok"          # a well-formed result with no error flag
STATUS_ERROR = "error"    # the server reported an error PROPERLY (protocol or tool)
STATUS_FAIL = "fail"      # no usable response came back (transport / empty)

ERR_NONE = "none"
ERR_TRANSPORT = "transport"
ERR_PROTOCOL = "protocol"
ERR_TOOL = "tool_error"

# Request headers we must never copy verbatim to the upstream (hop-by-hop per
# RFC 7230 plus Host/Content-Length, which the client library re-computes).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length",
}

_SSE_SPLIT = re.compile(r"\r?\n\r?\n")


# --- JSON-RPC request side ----------------------------------------------------
def parse_messages(raw) -> Tuple[List[dict], bool]:
    """Decode a streamable-HTTP POST body into JSON-RPC messages.

    Returns ``(messages, is_batch)``. A JSON array is a batch; a single object is
    wrapped in a one-element list with ``is_batch=False``. Anything that is not
    valid JSON (or not an object/array) yields ``([], False)`` so the shell can
    forward opaque bytes untouched and simply skip instrumentation, never 500.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return [], False
    if not isinstance(raw, str) or not raw.strip():
        return [], False
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return [], False
    if isinstance(doc, list):
        return [m for m in doc if isinstance(m, dict)], True
    if isinstance(doc, dict):
        return [doc], False
    return [], False


def is_request(msg: dict) -> bool:
    """A JSON-RPC request expects a response: it has a method AND a non-null id."""
    return isinstance(msg, dict) and "method" in msg and msg.get("id") is not None


def is_notification(msg: dict) -> bool:
    """A notification has a method but NO id: fire-and-forget, no response owed."""
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def request_method(msg: dict) -> str:
    m = msg.get("method")
    return m if isinstance(m, str) else ""


def tool_name(msg: dict) -> Optional[str]:
    """For ``tools/call`` the tool is ``params.name`` (the same field the probe
    stamps as ``mcp.tool.name``). Returns None for every other method."""
    if request_method(msg) != "tools/call":
        return None
    params = msg.get("params")
    if isinstance(params, dict):
        n = params.get("name")
        if isinstance(n, str):
            return n
    return None


def describe(msg: dict) -> dict:
    """Normalise one JSON-RPC message into the fields the proxy needs."""
    method = request_method(msg)
    return {
        "method": method,
        "id": msg.get("id"),
        "tool": tool_name(msg),
        "is_request": is_request(msg),
        "is_notification": is_notification(msg),
    }


def primary_message(messages: List[dict]) -> Optional[dict]:
    """The message a single span represents: the first real request if there is
    one (that is the interaction worth timing), else the first message at all."""
    for m in messages:
        if is_request(m):
            return m
    return messages[0] if messages else None


def span_name(method: str) -> str:
    """Match the probe's convention exactly: ``mcp.initialize`` etc."""
    return f"mcp.{method}" if method else "mcp.message"


def request_attrs(msg: dict, upstream_url: str, req_bytes: int,
                  batch_size: int = 1) -> dict:
    """The CLIENT-span attributes for a forwarded request, mirroring
    ``mcp2_probe._timed_call`` so proxy spans line up with hand-instrumented ones,
    plus ``mcp2.proxy`` so a viewer can tell auto-captured traffic apart."""
    d = describe(msg)
    attrs = {
        "mcp.transport": "streamable_http",
        "mcp.method": d["method"],
        "mcp.request.bytes": int(req_bytes),
        "mcp.server.url": upstream_url,
        "mcp2.proxy": True,
    }
    if d["tool"]:
        attrs["mcp.tool.name"] = d["tool"]
    if d["id"] is not None:
        attrs["mcp.jsonrpc.id"] = str(d["id"])
    if batch_size > 1:
        attrs["mcp.batch.size"] = int(batch_size)
    return attrs


# --- response side ------------------------------------------------------------
def parse_sse(text) -> List[dict]:
    """Extract JSON-RPC messages from a Server-Sent-Events body.

    SSE frames are separated by a blank line; the payload is the concatenation of
    the frame's ``data:`` lines. Comment lines (``:`` prefixed) and ``event:`` /
    ``id:`` fields are ignored. Undecodable payloads are skipped, never raised."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if not isinstance(text, str):
        return []
    out: List[dict] = []
    for frame in _SSE_SPLIT.split(text):
        data_lines = []
        for line in frame.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" "))
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
        elif isinstance(obj, list):
            out.extend(m for m in obj if isinstance(m, dict))
    return out


def parse_response_body(body, content_type: str) -> List[dict]:
    """Decode a response body into JSON-RPC messages, dispatching on content-type.

    ``text/event-stream`` is parsed as SSE; anything else is tried as a single
    JSON document (streamable-HTTP servers answer with one or the other)."""
    ct = (content_type or "").lower()
    if "text/event-stream" in ct:
        return parse_sse(body)
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if not isinstance(body, str) or not body.strip():
        return []
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return []
    if isinstance(doc, dict):
        return [doc]
    if isinstance(doc, list):
        return [m for m in doc if isinstance(m, dict)]
    return []


def _is_response(msg: dict) -> bool:
    return isinstance(msg, dict) and "method" not in msg and (
        "result" in msg or "error" in msg)


def _match_response(messages: List[dict], req_id) -> Optional[dict]:
    """The response whose id matches the request; falls back to the first response
    in the body when ids cannot be compared (some servers stringify ids)."""
    responses = [m for m in messages if _is_response(m)]
    if not responses:
        return None
    if req_id is not None:
        for m in responses:
            if m.get("id") == req_id:
                return m
        for m in responses:
            if str(m.get("id")) == str(req_id):
                return m
    return responses[0]


def _content_types(result: dict) -> List[str]:
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return []
    types = []
    for c in content:
        types.append(c.get("type", "unknown") if isinstance(c, dict) else "unknown")
    return types


def classify(response_messages: List[dict], req_msg: dict,
             http_ok: bool = True) -> dict:
    """Grade one interaction the same way ``mcp2_probe._timed_call`` does.

    Returns ``{status, error_class, is_error, content_types}``:

      * a JSON-RPC ``error`` object  -> error / protocol  (a proper protocol error
        IS a well-formed response, so this is not a transport failure).
      * a ``result`` with ``isError`` -> error / tool_error (the tool ran and
        reported failure).
      * a plain ``result``           -> ok / none.
      * a notification (no id owed)  -> ok / none (the POST was a 202, nothing to
        grade).
      * a request with no response   -> fail / transport (wedged or empty).
    """
    d = describe(req_msg)
    if not http_ok:
        return {"status": STATUS_FAIL, "error_class": ERR_TRANSPORT,
                "is_error": False, "content_types": []}
    if not d["is_request"]:
        # A notification or a client->server response: nothing is owed back.
        return {"status": STATUS_OK, "error_class": ERR_NONE,
                "is_error": False, "content_types": []}

    resp = _match_response(response_messages, d["id"])
    if resp is None:
        return {"status": STATUS_FAIL, "error_class": ERR_TRANSPORT,
                "is_error": False, "content_types": []}
    if "error" in resp and resp.get("error") is not None:
        return {"status": STATUS_ERROR, "error_class": ERR_PROTOCOL,
                "is_error": True, "content_types": []}

    result = resp.get("result")
    result = result if isinstance(result, dict) else {}
    if result.get("isError") is True:
        return {"status": STATUS_ERROR, "error_class": ERR_TOOL,
                "is_error": True, "content_types": _content_types(result)}
    return {"status": STATUS_OK, "error_class": ERR_NONE,
            "is_error": False, "content_types": _content_types(result)}


def response_attrs(grade: dict, latency_ms: Optional[float]) -> dict:
    """The CLIENT-span attributes derived from the graded response."""
    attrs = {
        "mcp.ok": grade["status"] != STATUS_FAIL,
        "mcp.is_error": bool(grade["is_error"]),
        "mcp.error.class": grade["error_class"],
        "mcp.content.types": ",".join(grade["content_types"]) or "none",
    }
    if latency_ms is not None:
        attrs["mcp.latency_ms"] = round(float(latency_ms), 2)
    return attrs


def filter_request_headers(headers) -> dict:
    """Drop hop-by-hop / Host / Content-Length before forwarding upstream."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def filter_response_headers(headers) -> dict:
    """Drop framing headers the streaming response will set for itself."""
    drop = {"content-length", "transfer-encoding", "connection", "keep-alive"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}

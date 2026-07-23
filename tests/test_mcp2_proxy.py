"""Unit tests for the MCP instrumentation proxy's pure core (mcp2_proxy_core.py).

The proxy forwards live MCP traffic, but every decision it makes about that
traffic -- what a request is, what tool it names, how big it is, whether the
response was a clean result, a proper protocol error, a tool error, or nothing at
all -- is pure and lives here. These tests build the JSON-RPC and Server-Sent-
Events bytes by hand and assert the grading matches the hand-written probe's
vocabulary (mcp2_probe / mcp2_metrics), with no server, no aiohttp and no SigNoz.
Importing the pure core must not drag in aiohttp or telemetry, which is asserted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp2_proxy_core as core


# --- request parsing ----------------------------------------------------------
def test_parse_single_request():
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    msgs, is_batch = core.parse_messages(raw)
    assert not is_batch and len(msgs) == 1
    assert msgs[0]["method"] == "tools/list"


def test_parse_batch():
    raw = b'[{"jsonrpc":"2.0","id":1,"method":"a"},{"jsonrpc":"2.0","method":"n"}]'
    msgs, is_batch = core.parse_messages(raw)
    assert is_batch and len(msgs) == 2


def test_parse_empty_and_garbage_are_safe():
    assert core.parse_messages(b"") == ([], False)
    assert core.parse_messages(b"not json") == ([], False)
    assert core.parse_messages(b"\xff\xfe\x00") == ([], False)   # invalid utf-8
    assert core.parse_messages("42") == ([], False)              # json but scalar


def test_is_request_vs_notification():
    req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert core.is_request(req) and not core.is_notification(req)
    assert core.is_notification(note) and not core.is_request(note)
    # id explicitly null is NOT a request expecting a response
    assert not core.is_request({"method": "x", "id": None})


def test_tool_name_only_for_tools_call():
    call = {"method": "tools/call", "id": 1, "params": {"name": "get_health"}}
    assert core.tool_name(call) == "get_health"
    assert core.tool_name({"method": "tools/list", "id": 1}) is None
    assert core.tool_name({"method": "tools/call", "id": 1, "params": {}}) is None


def test_primary_message_prefers_request():
    note = {"jsonrpc": "2.0", "method": "n"}
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "t"}}
    assert core.primary_message([note, req]) is req
    assert core.primary_message([note]) is note
    assert core.primary_message([]) is None


def test_span_name_matches_probe_convention():
    assert core.span_name("tools/call") == "mcp.tools/call"
    assert core.span_name("initialize") == "mcp.initialize"
    assert core.span_name("") == "mcp.message"


def test_request_attrs():
    call = {"method": "tools/call", "id": "abc", "params": {"name": "get_health"}}
    a = core.request_attrs(call, "http://up/mcp", req_bytes=42, batch_size=1)
    assert a["mcp.transport"] == "streamable_http"
    assert a["mcp.method"] == "tools/call"
    assert a["mcp.tool.name"] == "get_health"
    assert a["mcp.request.bytes"] == 42
    assert a["mcp.server.url"] == "http://up/mcp"
    assert a["mcp2.proxy"] is True
    assert a["mcp.jsonrpc.id"] == "abc"
    assert "mcp.batch.size" not in a


def test_request_attrs_batch_and_no_tool():
    listed = {"method": "tools/list", "id": 1}
    a = core.request_attrs(listed, "http://up/mcp", 10, batch_size=3)
    assert "mcp.tool.name" not in a
    assert a["mcp.batch.size"] == 3


# --- SSE / response parsing ---------------------------------------------------
def test_parse_sse_single_event():
    body = ("event: message\r\n"
            'data: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\r\n\r\n')
    msgs = core.parse_sse(body)
    assert len(msgs) == 1 and msgs[0]["id"] == 1


def test_parse_sse_multiline_data_and_comments():
    body = (": this is a comment\n"
            'data: {"jsonrpc":"2.0",\n'
            'data: "id":5,"result":{}}\n\n')
    msgs = core.parse_sse(body)
    assert len(msgs) == 1 and msgs[0]["id"] == 5


def test_parse_sse_skips_undecodable_frames():
    body = ('data: not-json\n\n'
            'data: {"jsonrpc":"2.0","id":9,"result":{}}\n\n')
    msgs = core.parse_sse(body)
    assert len(msgs) == 1 and msgs[0]["id"] == 9


def test_parse_response_body_dispatch():
    js = '{"jsonrpc":"2.0","id":1,"result":{}}'
    assert core.parse_response_body(js, "application/json")[0]["id"] == 1
    sse = 'data: {"jsonrpc":"2.0","id":2,"result":{}}\n\n'
    assert core.parse_response_body(sse, "text/event-stream")[0]["id"] == 2
    assert core.parse_response_body("", "application/json") == []


# --- grading (mirrors mcp2_probe._timed_call) ---------------------------------
def _resp(rid, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        m["error"] = error
    else:
        m["result"] = result or {}
    return m


def test_classify_clean_result_ok():
    req = {"method": "tools/call", "id": 1, "params": {"name": "t"}}
    resp = [_resp(1, {"content": [{"type": "text"}, {"type": "resource"}]})]
    g = core.classify(resp, req)
    assert g["status"] == core.STATUS_OK
    assert g["error_class"] == core.ERR_NONE
    assert g["is_error"] is False
    assert g["content_types"] == ["text", "resource"]


def test_classify_tool_error():
    req = {"method": "tools/call", "id": 1, "params": {"name": "t"}}
    resp = [_resp(1, {"isError": True, "content": [{"type": "text"}]})]
    g = core.classify(resp, req)
    assert g["status"] == core.STATUS_ERROR
    assert g["error_class"] == core.ERR_TOOL
    assert g["is_error"] is True


def test_classify_protocol_error():
    req = {"method": "tools/call", "id": 2, "params": {"name": "t"}}
    resp = [_resp(2, error={"code": -32602, "message": "bad params"})]
    g = core.classify(resp, req)
    assert g["status"] == core.STATUS_ERROR
    assert g["error_class"] == core.ERR_PROTOCOL
    assert g["is_error"] is True


def test_classify_no_response_is_transport_fail():
    req = {"method": "tools/call", "id": 3, "params": {"name": "t"}}
    g = core.classify([], req)
    assert g["status"] == core.STATUS_FAIL
    assert g["error_class"] == core.ERR_TRANSPORT


def test_classify_notification_owes_nothing():
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    g = core.classify([], note)
    assert g["status"] == core.STATUS_OK
    assert g["error_class"] == core.ERR_NONE


def test_classify_http_transport_failure():
    req = {"method": "tools/list", "id": 1}
    g = core.classify([], req, http_ok=False)
    assert g["status"] == core.STATUS_FAIL
    assert g["error_class"] == core.ERR_TRANSPORT


def test_classify_matches_response_by_id_in_batch():
    req = {"method": "tools/call", "id": 2, "params": {"name": "t"}}
    resp = [_resp(1, {"content": []}), _resp(2, error={"code": -1, "message": "x"})]
    g = core.classify(resp, req)
    assert g["is_error"] is True and g["error_class"] == core.ERR_PROTOCOL


def test_response_attrs():
    g = {"status": core.STATUS_OK, "error_class": core.ERR_NONE,
         "is_error": False, "content_types": ["text"]}
    a = core.response_attrs(g, 12.345)
    assert a["mcp.ok"] is True
    assert a["mcp.is_error"] is False
    assert a["mcp.error.class"] == "none"
    assert a["mcp.content.types"] == "text"
    assert a["mcp.latency_ms"] == 12.35
    # a fail marks mcp.ok False and defaults content types to "none"
    gf = {"status": core.STATUS_FAIL, "error_class": core.ERR_TRANSPORT,
          "is_error": False, "content_types": []}
    af = core.response_attrs(gf, None)
    assert af["mcp.ok"] is False and af["mcp.content.types"] == "none"
    assert "mcp.latency_ms" not in af


# --- header hygiene -----------------------------------------------------------
def test_filter_request_headers_drops_hop_by_hop():
    h = {"Host": "proxy:8009", "Content-Length": "10", "Connection": "keep-alive",
         "Accept": "application/json, text/event-stream",
         "Mcp-Session-Id": "s-1", "Content-Type": "application/json"}
    out = core.filter_request_headers(h)
    assert "Host" not in out and "Content-Length" not in out and "Connection" not in out
    assert out["Accept"].startswith("application/json")
    assert out["Mcp-Session-Id"] == "s-1"
    assert out["Content-Type"] == "application/json"


def test_filter_response_headers_drops_framing():
    h = {"Content-Length": "5", "Transfer-Encoding": "chunked",
         "Content-Type": "text/event-stream", "Mcp-Session-Id": "s-2"}
    out = core.filter_response_headers(h)
    assert "Content-Length" not in out and "Transfer-Encoding" not in out
    assert out["Content-Type"] == "text/event-stream"
    assert out["Mcp-Session-Id"] == "s-2"


def test_pure_core_has_no_heavy_imports():
    # The pure core must stay importable with no aiohttp / telemetry / OTel, so it
    # can be unit-tested and reasoned about in isolation (like mcp2_model).
    import mcp2_proxy_core as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "import aiohttp" not in src
    assert "import telemetry" not in src
    assert "opentelemetry" not in src

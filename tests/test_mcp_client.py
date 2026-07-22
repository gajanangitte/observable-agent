"""Offline tests for the SigNozMCP sync facade's response handling. The MCP SDK is
async and needs a live server, so we stub the private ``_run`` to return canned result
objects: this isolates ``call_tool``'s text extraction and its isError fail-closed
behaviour without any network or event loop."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_client


class _Text:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, content, isError=False):
        self.content = content
        self.isError = isError


def _client(result):
    c = mcp_client.SigNozMCP("http://fake/mcp")
    c._run = lambda coro_fn: result   # bypass the async streamable-HTTP transport
    return c


def test_call_tool_concatenates_text_content():
    c = _client(_Result([_Text("line 1"), _Text("line 2")]))
    assert c.call_tool("signoz_x", {}) == "line 1\nline 2"


def test_call_tool_ignores_non_text_content():
    class _Img:
        type = "image"

    c = _client(_Result([_Text("keep"), _Img()]))
    assert c.call_tool("signoz_x", {}) == "keep"


def test_call_tool_raises_on_iserror():
    # A tool that FAILED (isError) must RAISE, not return its error text as a normal
    # result, so the fail-closed sensors convert it to UNKNOWN rather than scoring the
    # error string as real data (a false, comforting read).
    c = _client(_Result([_Text("boom: bad query")], isError=True))
    raised = False
    try:
        c.call_tool("signoz_x", {})
    except RuntimeError as e:
        raised = True
        assert "boom: bad query" in str(e)
    assert raised


def test_call_tool_raises_on_iserror_without_text():
    # isError true but no text content: still raise, with a synthesised message that
    # names the tool, so the failure is never silently swallowed.
    c = _client(_Result([], isError=True))
    raised = False
    try:
        c.call_tool("signoz_y", {})
    except RuntimeError as e:
        raised = True
        assert "signoz_y" in str(e)
    assert raised


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()

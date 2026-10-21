import json
from pathlib import Path

import anyio
import pytest
from starlette.testclient import TestClient

from ethtester.config import Config
from ethtester.checks.command import CheckError
from ethtester.mcp.auth import BearerAuth, read_token
from ethtester.mcp.server import INSTRUCTIONS, build_app, resolve_bind
from ethtester.mcp.service import ProbeError

TOKEN = "s3cret-token"
HOST = "http://eth.tail1234.ts.net:8765"
EXPECTED_TOOLS = {"get_status", "get_history", "get_link_details", "ping", "traceroute", "dns_lookup",
                  "http_check", "dhcp_renew", "link_control", "neighbour_scan", "speed_test"}


class FakeService:
    def get_status(self):
        return {"verdict": "OK", "stale": False}

    def ping(self, host, count=5, size=56):
        if host == "unplugged":
            raise ProbeError("eth0 has no link; plug in a cable first")
        return {"host": host, "count": count}


def post(client, method, params=None, token=TOKEN):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers,
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


@pytest.fixture
def client():
    with TestClient(build_app(FakeService(), TOKEN), base_url=HOST) as c:
        yield c


def test_rejects_missing_or_wrong_token(client):
    assert post(client, "tools/list", token=None).status_code == 401
    assert post(client, "tools/list", token="wrong").status_code == 401


def test_lists_all_tools(client):
    response = post(client, "tools/list")
    assert response.status_code == 200
    assert {t["name"] for t in response.json()["result"]["tools"]} == EXPECTED_TOOLS


def test_calls_tool(client):
    result = post(client, "tools/call", {"name": "get_status", "arguments": {}}).json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["verdict"] == "OK"
    result = post(client, "tools/call", {"name": "ping", "arguments": {"host": "1.1.1.1", "count": 3}}).json()["result"]
    assert json.loads(result["content"][0]["text"]) == {"host": "1.1.1.1", "count": 3}


def test_tool_error_is_reported(client):
    result = post(client, "tools/call", {"name": "ping", "arguments": {"host": "unplugged"}}).json()["result"]
    assert result["isError"] is True
    assert "no link" in result["content"][0]["text"]


def test_read_token(tmp_path: Path):
    path = tmp_path / "token"
    path.write_text("abc123\n")
    assert read_token(path) == "abc123"
    path.write_text("\n")
    with pytest.raises(SystemExit):
        read_token(path)
    with pytest.raises(SystemExit):
        read_token(tmp_path / "missing")


def test_resolve_bind():
    assert resolve_bind(Config(mcp_bind="100.64.0.7")) == "100.64.0.7"
    assert resolve_bind(Config(), run=lambda cmd, **kw: "100.101.102.103\n") == "100.101.102.103"
    with pytest.raises(SystemExit, match="tailscale up"):
        resolve_bind(Config(), run=lambda cmd, **kw: "")
    with pytest.raises(ValueError):
        resolve_bind(Config(mcp_bind="0.0.0.0x"))


def test_resolve_bind_rejects_unspecified_address():
    with pytest.raises(ValueError, match="0.0.0.0"):
        resolve_bind(Config(mcp_bind="0.0.0.0"))
    with pytest.raises(SystemExit, match="0.0.0.0"):
        resolve_bind(Config(), run=lambda cmd, **kw: "0.0.0.0\n")


def test_resolve_bind_tailscale_unavailable():
    def failing_run(cmd, **kw):
        raise CheckError("tailscale: not found")

    with pytest.raises(SystemExit, match="Tailscale isn't available"):
        resolve_bind(Config(), run=failing_run)


def test_bearer_auth_closes_websocket_without_calling_app():
    calls = []
    sent = []

    async def inner(scope, receive, send):
        calls.append(scope["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    anyio.run(BearerAuth(inner, TOKEN), {"type": "websocket", "headers": []}, receive, send)
    assert calls == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_bearer_auth_passes_lifespan_through():
    calls = []

    async def inner(scope, receive, send):
        calls.append(scope["type"])

    async def noop(*args):
        return None

    anyio.run(BearerAuth(inner, TOKEN), {"type": "lifespan"}, noop, noop)
    assert calls == ["lifespan"]


def call_http(client_addr, token=TOKEN):
    calls = []
    sent = []

    async def inner(scope, receive, send):
        calls.append(scope.get("client"))

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {"type": "http", "headers": headers}
    if client_addr != "absent":
        scope["client"] = client_addr
    anyio.run(BearerAuth(inner, TOKEN), scope, receive, send)
    return calls, sent


def test_bearer_auth_rejects_non_tailscale_client_before_token_check():
    calls, sent = call_http(("192.168.1.50", 1234))
    assert calls == []
    assert sent[0]["status"] == 403
    calls, sent = call_http(("192.168.1.50", 1234), token=None)
    assert calls == [] and sent[0]["status"] == 403


@pytest.mark.parametrize("addr", ["100.101.102.103", "100.64.0.1", "127.0.0.1", "::1", "fd7a:115c:a1e0::1234"])
def test_bearer_auth_allows_tailscale_and_loopback_clients(addr):
    calls, sent = call_http((addr, 1234))
    assert calls == [(addr, 1234)] and sent == []


@pytest.mark.parametrize("addr", ["100.128.0.1", "10.0.0.5", "fd7a:115c:a1e1::1", "2001:db8::1", "example.com"])
def test_bearer_auth_rejects_other_clients(addr):
    calls, sent = call_http((addr, 1234))
    assert calls == [] and sent[0]["status"] == 403


def test_bearer_auth_tailscale_client_still_needs_token():
    calls, sent = call_http(("100.101.102.103", 1234), token="wrong")
    assert calls == [] and sent[0]["status"] == 401


@pytest.mark.parametrize("client_addr", ["absent", None, ("testclient", 50000)])
def test_bearer_auth_allows_missing_client_and_testclient(client_addr):
    calls, sent = call_http(client_addr)
    assert len(calls) == 1 and sent == []


def test_instructions_warn_that_disruptive_tools_show_up_on_screen_and_history():
    text = " ".join(INSTRUCTIONS.split())
    assert ("dhcp_renew and link_control briefly show up on the tester's own screen and in get_history "
            "(e.g. NO DHCP, CABLE? (10M), extra flaps); treat those entries as caused by the test.") in text

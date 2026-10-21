import io
import json
import os
import stat
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from ethtester import cli

URL = "http://tester:8765/mcp"
TOKEN = "s3cret-token"


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def tool_result(text, is_error=False):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}


class FakeOpener:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.requests, self.timeouts = payload, exc, [], []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.exc:
            raise self.exc
        return FakeResponse(self.payload)

    @property
    def body(self):
        return json.loads(self.requests[-1].data)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("ETHTESTER_URL", "ETHTESTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ETHTESTER_CONFIG", str(tmp_path / "cfg" / "cli.toml"))


def run(argv, opener, capsys):
    code = cli.main(["--url", URL, "--token", TOKEN, *argv], opener=opener)
    return code, capsys.readouterr()


def test_request_shape():
    opener = FakeOpener(tool_result("{}"))
    cli.call_tool(URL, TOKEN, "get_status", {}, 7, opener)
    request = opener.requests[0]
    assert request.full_url == URL and request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json, text/event-stream"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert opener.body == {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "get_status", "arguments": {}}}
    assert opener.timeouts == [7]


@pytest.mark.parametrize("argv,tool,arguments", [
    (["status"], "get_status", {}),
    (["history"], "get_history", {}),
    (["history", "--limit", "5", "--since", "2026-01-01T00:00:00"], "get_history",
     {"limit": 5, "since": "2026-01-01T00:00:00"}),
    (["link"], "get_link_details", {}),
    (["ping", "1.1.1.1"], "ping", {"host": "1.1.1.1"}),
    (["ping", "1.1.1.1", "-c", "3", "-s", "100"], "ping", {"host": "1.1.1.1", "count": 3, "size": 100}),
    (["traceroute", "h", "--max-hops", "8"], "traceroute", {"host": "h", "max_hops": 8}),
    (["dns", "example.com"], "dns_lookup", {"name": "example.com"}),
    (["dns", "example.com", "-t", "MX", "--server", "9.9.9.9"], "dns_lookup",
     {"name": "example.com", "record_type": "MX", "server": "9.9.9.9"}),
    (["http", "https://x.y"], "http_check", {"url": "https://x.y"}),
    (["renew-dhcp"], "dhcp_renew", {}),
    (["link-control", "renegotiate"], "link_control", {"action": "renegotiate"}),
    (["link-control", "force-10", "--duration", "30"], "link_control", {"action": "force_10", "duration_s": 30}),
    (["link-control", "force-100"], "link_control", {"action": "force_100"}),
    (["neighbours"], "neighbour_scan", {}),
    (["speedtest", "10.0.0.2"], "speed_test", {"server": "10.0.0.2"}),
    (["speedtest", "10.0.0.2", "--seconds", "5", "--reverse"], "speed_test",
     {"server": "10.0.0.2", "seconds": 5, "reverse": True}),
])
def test_argument_mapping(argv, tool, arguments, capsys):
    opener = FakeOpener(tool_result('{"ok": true}'))
    code, out = run(argv, opener, capsys)
    assert code == 0
    assert opener.body["params"] == {"name": tool, "arguments": arguments}
    assert json.loads(out.out) == {"ok": True}


def test_pretty_json_and_plain_text(capsys):
    code, out = run(["status"], FakeOpener(tool_result('{"a":1}')), capsys)
    assert out.out == '{\n  "a": 1\n}\n'
    code, out = run(["status"], FakeOpener(tool_result("not json")), capsys)
    assert (code, out.out) == (0, "not json\n")


def test_tool_error_exits_1(capsys):
    code, out = run(["ping", "x"], FakeOpener(tool_result("eth0 has no link", True)), capsys)
    assert code == 1 and out.out == "" and "no link" in out.err


def test_unauthorized_exits_2(capsys):
    for status in (401, 403):
        err = urllib.error.HTTPError(URL, status, "x", {}, None)
        code, out = run(["status"], FakeOpener(exc=err), capsys)
        assert code == 2
        assert out.err.strip() == "unauthorized: check the token (ethtester configure)"


def test_connection_error_exits_2(capsys):
    code, out = run(["status"], FakeOpener(exc=urllib.error.URLError("refused")), capsys)
    assert code == 2 and out.err.strip() == f"cannot reach {URL}: refused"
    code, out = run(["status"], FakeOpener(exc=TimeoutError("timed out")), capsys)
    assert code == 2 and "cannot reach" in out.err and "timed out" in out.err


def test_jsonrpc_error_exits_2(capsys):
    payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}
    code, out = run(["status"], FakeOpener(payload), capsys)
    assert code == 2 and "bad params" in out.err


def test_timeout_flag(capsys):
    opener = FakeOpener(tool_result("{}"))
    run(["status"], opener, capsys)
    assert opener.timeouts == [120.0]
    cli.main(["--url", URL, "--token", TOKEN, "--timeout", "5", "status"], opener=opener)
    assert opener.timeouts[-1] == 5.0


def test_tools_lists_first_docstring_line(capsys):
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [
        {"name": "ping", "description": "Ping a host.\nMore detail."}, {"name": "x", "description": ""}]}}
    opener = FakeOpener(payload)
    code, out = run(["tools"], opener, capsys)
    assert code == 0 and opener.body["method"] == "tools/list"
    assert out.out.splitlines()[0] == "ping: Ping a host."


def test_missing_config_exits_2(capsys):
    code = cli.main(["status"], opener=FakeOpener(tool_result("{}")))
    err = capsys.readouterr().err
    assert code == 2 and "ethtester configure" in err


def write_cfg(path: Path, url, token):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'url = "{url}"\ntoken = "{token}"\n')


def test_resolution_order(monkeypatch, tmp_path):
    cfg = Path(os.environ["ETHTESTER_CONFIG"])
    write_cfg(cfg, "http://file", "filetok")
    assert cli.resolve_settings() == ("http://file", "filetok")
    monkeypatch.setenv("ETHTESTER_URL", "http://env")
    monkeypatch.setenv("ETHTESTER_TOKEN", "envtok")
    assert cli.resolve_settings() == ("http://env", "envtok")
    assert cli.resolve_settings("http://flag", "flagtok") == ("http://flag", "flagtok")
    assert cli.resolve_settings("http://flag") == ("http://flag", "envtok")


def test_configure_writes_private_file_and_hides_token(capsys, monkeypatch):
    path = Path(os.environ["ETHTESTER_CONFIG"])
    monkeypatch.setattr("sys.stdin", io.StringIO("piped-token\n"))
    assert cli.main(["configure", "--url", URL]) == 0
    out = capsys.readouterr()
    assert "piped-token" not in out.out + out.err and str(path) in out.out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert cli.resolve_settings() == (URL, "piped-token")
    assert cli.main(["configure", "--url", URL, "--token", "flag-token"]) == 0
    assert "flag-token" not in capsys.readouterr().out
    assert cli.resolve_settings() == (URL, "flag-token")


def test_configure_without_token_exits_2(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["configure", "--url", URL]) == 2


def test_help_mentions_essentials(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    for needle in ("Ethernet port", "JSON", "Exit codes", "renew-dhcp", "link-control", "100 Mbit"):
        assert needle in text


def test_wrapper_runs_through_symlink(tmp_path):
    import subprocess
    import sys
    link = tmp_path / "ethtester"
    link.symlink_to(Path(__file__).resolve().parent.parent / "scripts" / "ethtester")
    done = subprocess.run([sys.executable, str(link), "--help"], capture_output=True, text=True)
    assert done.returncode == 0 and "Exit codes" in done.stdout


# --- integration against the real MCP app ------------------------------------------------------

class FakeService:
    def get_status(self):
        return {"verdict": "OK", "stale": False}

    def ping(self, host, count=5, size=56):
        return {"host": host, "count": count}


@pytest.fixture
def server():
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("mcp")
    import socket
    from ethtester.mcp.server import build_app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(build_app(FakeService(), TOKEN), host="127.0.0.1", port=port,
                                        log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    thread.join(timeout=5)


def test_integration(server, capsys):
    assert cli.main(["--url", server, "--token", TOKEN, "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"verdict": "OK", "stale": False}
    assert cli.main(["--url", server, "--token", TOKEN, "ping", "1.1.1.1", "-c", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"host": "1.1.1.1", "count": 3}
    assert cli.main(["--url", server, "--token", TOKEN, "tools"]) == 0
    assert "get_status:" in capsys.readouterr().out
    assert cli.main(["--url", server, "--token", "wrong", "status"]) == 2
    assert "unauthorized" in capsys.readouterr().err

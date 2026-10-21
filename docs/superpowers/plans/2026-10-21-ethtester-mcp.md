# Ethernet Tester MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An HTTP MCP server on the tester Pi. Claude Code on the user's machines reaches it over Tailscale with a bearer token, and can read the tester's status and run probes through the Ethernet dongle.

**Architecture:** The existing `ethtester` service also writes `/run/ethtester/state.json` on every tick. A new `ethtester-mcp` service (package `ethtester/mcp/`) runs FastMCP over streamable HTTP, wrapped in a bearer-token ASGI middleware, and listens only on the Pi's Tailscale IPv4 address. Each tool calls `ProbeService`, which validates input, checks the link and runs a probe. Probes are argv lists bound to `eth0`, never a shell. Every call is logged to `probes.log`. Link and DHCP actions share one lock, and forced speeds revert automatically.

**Tech Stack:** Python 3.11+, the `mcp` Python SDK (FastMCP, `>=1.13,<2`), uvicorn, anyio, Starlette TestClient (tests), plus the Linux tools `ethtool`, `traceroute`, `iperf3`, `arp-scan`, `curl`, `nmcli`, and Tailscale.

**Spec:** `docs/superpowers/specs/2026-10-21-ethtester-mcp-design.md`

## Global Constraints

- Code must run on Python 3.11 (the Pi runs Python 3.13 on Debian 13 Trixie). Use no syntax or library features newer than 3.11.
- Never pass a command through a shell. Every external command is an argv list run via `run_command` (or an injected `run`) with a timeout.
- Every network probe is bound to the configured interface (default `eth0`): `ping -I`, `traceroute -i`, `curl --interface`, `iperf3 --bind-dev`, `arp-scan --interface`, and `SO_BINDTODEVICE` for DNS.
- The server listens on `cfg.mcp_bind`: "tailscale" means the first address from `tailscale ip -4`, otherwise an explicit IPv4. Port `cfg.mcp_port` defaults to 8765. Never listen on 0.0.0.0.
- Auth: every HTTP request needs `Authorization: Bearer <token>`, compared in constant time. Otherwise the server returns 401. The token is read from `/etc/ethtester/mcp-token`.
- Tool limits (verbatim from the spec):
  - ping count 1–20 (default 5), size 16–1472 (default 56);
  - traceroute max_hops 1–30 (default 20);
  - history limit 1–200 (default 50);
  - link_control duration_s 10–300 (default 60);
  - speed_test seconds 3–30 (default 10);
  - DNS types A, AAAA, CNAME, MX, NS, TXT;
  - http_check accepts http/https only, with a 15 s timeout;
  - neighbour_scan only when the prefix is /22 or smaller.
- `dhcp_renew`, `link_control` and `neighbour_scan` share one lock. A second call gets a "busy" error.
- `get_status` reports `stale` when the state file is more than 30 s old.
- Development on the Mac: `.venv/bin/pytest`. Task 7 adds `mcp` and `uvicorn` to `.venv`.
- End each commit message with a blank line plus the Co-Authored-By attribution line for the model doing the work.

## File Structure

```
ethtester/
  statefile.py            write_state(): atomic /run/ethtester/state.json
  config.py               (modify) add state_path, mcp_port, mcp_bind
  main.py                 (modify) run() gets write_state=; main() wires it
  mcp/
    __init__.py           empty
    __main__.py           python -m ethtester.mcp
    validate.py           input validators
    dnsq.py               DNS query/parse for A/AAAA/CNAME/MX/NS/TXT
    probes.py             probe argv builders + output parsers
    linkctl.py            LinkControl: lock, renegotiate, force + auto-revert, recover()
    probelog.py           ProbeLog: JSON lines to probes.log
    service.py            ProbeService: one method per tool
    auth.py               BearerAuth ASGI middleware, read_token()
    server.py             build_app(), resolve_bind(), main()
deploy/
  ethtester-mcp.service   new unit
  install.sh              (modify) packages, Tailscale, venv, token, unit, hints
README.md                 (modify) "Remote probes (MCP)" section
pyproject.toml            (modify) optional dependency group "mcp"
tests/
  test_statefile.py, test_main.py (append)
  test_mcp_validate.py, test_mcp_dnsq.py, test_mcp_probes.py,
  test_mcp_linkctl.py, test_mcp_service.py, test_mcp_server.py
```

---

### Task 1: State file from the main service

**Files:**
- Create: `ethtester/statefile.py`
- Modify: `ethtester/config.py` (add three `Config` fields), `ethtester/main.py`
- Test: `tests/test_statefile.py`, `tests/test_main.py` (append)

**Interfaces:**
- Consumes: `Snapshot`, `Verdict` (existing), `run()` and `main()` in `ethtester/main.py` (existing).
- Produces:
  - `write_state(path: Path, snapshot, verdict, wall_time: float) -> None` writes `{"ts", "epoch", "verdict", "text", "snapshot"}`;
  - new `Config` fields `state_path: str = "/run/ethtester/state.json"`, `mcp_port: int = 8765`, `mcp_bind: str = "tailscale"`;
  - `run(..., write_state=None)`, where `write_state` is called as `write_state(snapshot, verdict, now)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_statefile.py`:
```python
import json
from pathlib import Path

from ethtester.statefile import write_state
from ethtester.verdict import evaluate
from tests.factories import ok_snapshot


def test_write_state(tmp_path: Path):
    path = tmp_path / "run" / "state.json"
    s = ok_snapshot()
    write_state(path, s, evaluate(s, 1000, 20), 1_700_000_000.5)
    data = json.loads(path.read_text())
    assert data["verdict"] == "OK"
    assert data["text"] == "OK"
    assert data["epoch"] == 1_700_000_000.5
    assert data["snapshot"]["dhcp"]["ip"] == "192.168.1.57"
    assert "ts" in data
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_write_state_replaces(tmp_path: Path):
    path = tmp_path / "state.json"
    s = ok_snapshot()
    write_state(path, s, evaluate(s, 1000, 20), 1.0)
    write_state(path, s, evaluate(s, 1000, 20), 2.0)
    assert json.loads(path.read_text())["epoch"] == 2.0
```

Append to `tests/test_main.py`:
```python
from ethtester.config import Config as _Config
from ethtester.display import Display as _Display, PngBackend as _PngBackend
from ethtester.main import run as _run
from ethtester.statelog import StateLogger as _StateLogger


def test_run_writes_state_and_survives_state_failure(tmp_path):
    cfg = _Config(display="png", png_path=str(tmp_path / "s.png"))
    written = []
    _run(cfg, ScriptedCollector(ok_snapshot()), _Display(_PngBackend(Path(cfg.png_path))),
         _StateLogger(tmp_path / "a.log"), sleep=lambda d: None, wall=lambda: 1.0, ticks=1,
         notify=lambda m: None, write_state=lambda s, v, now: written.append((v.code, now)))
    assert written == [("OK", 1.0)]

    def failing(s, v, now):
        raise OSError("read-only")

    png = tmp_path / "t.png"
    _run(cfg, ScriptedCollector(ok_snapshot()), _Display(_PngBackend(png)),
         _StateLogger(tmp_path / "b.log"), sleep=lambda d: None, wall=lambda: 1.0, ticks=1,
         notify=lambda m: None, write_state=failing)
    assert png.exists()
```
(`ScriptedCollector`, `Path` and `ok_snapshot` are already defined or imported in `tests/test_main.py`. If `Path` isn't imported there, add `from pathlib import Path`.)

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/pytest tests/test_statefile.py tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.statefile'`, plus `TypeError: run() got an unexpected keyword argument 'write_state'`.

- [ ] **Step 3: Implement**

`ethtester/statefile.py`:
```python
"""Latest status for other processes (the MCP server), replaced atomically each tick."""

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


def write_state(path: Path, snapshot, verdict, wall_time: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ts": datetime.fromtimestamp(wall_time).isoformat(timespec="seconds"),
        "epoch": wall_time,
        "verdict": verdict.code,
        "text": verdict.text,
        "snapshot": asdict(snapshot),
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
```

In `ethtester/config.py`, add these three fields at the end of `Config`:
```python
    state_path: str = "/run/ethtester/state.json"
    mcp_port: int = 8765
    mcp_bind: str = "tailscale"
```
and this entry in `LIMITS`:
```python
          "mcp_port": (1, False),
```

In `ethtester/main.py`:
- add `import functools` and `from .statefile import write_state as write_state_file`;
- add `write_state=None` as the last parameter of `run()`;
- insert this block directly after the existing `statelog.record` try/except:
```python
            if write_state is not None:
                try:
                    write_state(snapshot, verdict, now)
                except Exception:
                    log.exception("state file write failed")
```
- change the last line of `main()` to:
```python
    run(cfg, Collector(cfg), display, StateLogger(Path(cfg.log_path)),
        write_state=functools.partial(write_state_file, Path(cfg.state_path)))
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass (the previous 102 plus 3 new).

- [ ] **Step 5: Commit**

```bash
git add ethtester/statefile.py ethtester/config.py ethtester/main.py tests/test_statefile.py tests/test_main.py
git commit -m "feat: publish the latest status to /run/ethtester/state.json"
```

---

### Task 2: Input validation

**Files:**
- Create: `ethtester/mcp/__init__.py` (empty), `ethtester/mcp/validate.py`
- Test: `tests/test_mcp_validate.py`

**Interfaces:**
- Produces:
  - `RECORD_TYPES: dict[str, int]` = `{"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28}`
  - `host(value) -> str`
  - `ipv4(value) -> str`
  - `bounded(name, value, low, high) -> int`
  - `url(value) -> str`
  - `record_type(value) -> str`
  - `timestamp(value) -> datetime` (naive local time)
  - `boolean(name, value) -> bool`
- Every validator raises `ValueError` with a message that names the problem.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_validate.py`:
```python
from datetime import datetime, timezone

import pytest

from ethtester.mcp import validate


@pytest.mark.parametrize("value", ["example.com", "ap-1.lan", "192.168.1.1", "2001:db8::1", "localhost", "a.b.c."])
def test_host_accepts(value):
    assert validate.host(value) == value


@pytest.mark.parametrize("value", ["", "-f", "a;b", "a b", "$(reboot)", "a..b", "x" * 254, "-c 5 1.1.1.1"])
def test_host_rejects(value):
    with pytest.raises(ValueError):
        validate.host(value)


def test_ipv4():
    assert validate.ipv4("10.0.0.1") == "10.0.0.1"
    for bad in ("::1", "dns.google", "10.0.0.256"):
        with pytest.raises(ValueError):
            validate.ipv4(bad)


def test_bounded():
    assert validate.bounded("count", 5, 1, 20) == 5
    for bad in (0, 21, True, 2.5, "5"):
        with pytest.raises(ValueError, match="count"):
            validate.bounded("count", bad, 1, 20)


def test_url():
    assert validate.url("https://example.com/x?y=1") == "https://example.com/x?y=1"
    for bad in ("ftp://example.com", "file:///etc/passwd", "https://", "http://-x/", "https://a b.com", "example.com"):
        with pytest.raises(ValueError):
            validate.url(bad)


def test_record_type():
    assert validate.record_type("aaaa") == "AAAA"
    with pytest.raises(ValueError):
        validate.record_type("ANY")


def test_timestamp():
    assert validate.timestamp("2026-10-21T09:40:00") == datetime(2026, 10, 21, 9, 40)
    aware = validate.timestamp("2026-10-21T07:40:00+00:00")
    assert aware.tzinfo is None
    assert aware == datetime(2026, 10, 21, 7, 40, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    with pytest.raises(ValueError):
        validate.timestamp("yesterday")


def test_boolean():
    assert validate.boolean("reverse", True) is True
    with pytest.raises(ValueError):
        validate.boolean("reverse", "yes")
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/pytest tests/test_mcp_validate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.mcp'`

- [ ] **Step 3: Implement**

`ethtester/mcp/__init__.py`: an empty file.

`ethtester/mcp/validate.py`:
```python
"""Input checks for MCP tool arguments; anything reaching a command line passes through here."""

import ipaddress
import re
from datetime import datetime
from urllib.parse import urlsplit

RECORD_TYPES = {"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28}
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){LABEL}(?:\.{LABEL})*\.?$")


def host(value) -> str:
    if not isinstance(value, str):
        raise ValueError(f"host must be a string, got {value!r}")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if HOSTNAME_RE.match(value):
        return value
    raise ValueError(f"not a hostname or IP address: {value!r}")


def ipv4(value) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except (ValueError, TypeError):
        raise ValueError(f"not an IPv4 address: {value!r}") from None


def bounded(name: str, value, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}, got {value!r}")
    return value


def url(value) -> str:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError(f"not a valid URL: {value!r}")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"URL must be http:// or https:// with a host, got {value!r}")
    host(parts.hostname)
    return value


def record_type(value) -> str:
    upper = str(value).upper()
    if upper not in RECORD_TYPES:
        raise ValueError(f"record type must be one of {', '.join(RECORD_TYPES)}, got {value!r}")
    return upper


def timestamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"not an ISO timestamp: {value!r}") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def boolean(name: str, value) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/pytest tests/test_mcp_validate.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ethtester/mcp/__init__.py ethtester/mcp/validate.py tests/test_mcp_validate.py
git commit -m "feat(mcp): input validators for probe arguments"
```

---

### Task 3: DNS lookups of any common record type

**Files:**
- Create: `ethtester/mcp/dnsq.py`
- Test: `tests/test_mcp_dnsq.py`

**Interfaces:**
- Consumes: `SO_BINDTODEVICE` from `ethtester/checks/dns.py`, and `RECORD_TYPES` (Task 2).
- Produces:
  - `build_query(name: str, qtype: int, qid: int) -> bytes`
  - `read_name(data: bytes, offset: int) -> tuple[str, int]`
  - `parse_answer(data: bytes, qid: int) -> dict`, which returns `{"rcode": str, "answers": [{"name", "type", "ttl", "data"}]}` and raises `ValueError` on malformed data
  - `lookup(server: str, name: str, qtype: str, interface: str, timeout: float = 3.0, clock=time.monotonic) -> dict`, which also adds `server` and `ms`

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_dnsq.py`:
```python
import struct

import pytest

from ethtester.mcp.dnsq import build_query, parse_answer, read_name

QNAME = b"\x07example\x03com\x00"


def response(qid, flags, qtype, answers):
    head = struct.pack(">HHHHHH", qid, flags, 1, len(answers), 0, 0)
    body = QNAME + struct.pack(">HH", qtype, 1)
    for rtype, ttl, rdata in answers:
        body += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, ttl, len(rdata)) + rdata
    return head + body


def test_build_query_mx():
    assert build_query("example.com", 15, 0x1234) == (
        bytes.fromhex("123401000001000000000000") + QNAME + b"\x00\x0f\x00\x01")


def test_parse_a_and_aaaa():
    data = response(7, 0x8180, 1, [(1, 300, bytes([93, 184, 216, 34])), (28, 60, b"\x00" * 15 + b"\x01")])
    assert parse_answer(data, 7) == {"rcode": "NOERROR", "answers": [
        {"name": "example.com", "type": "A", "ttl": 300, "data": "93.184.216.34"},
        {"name": "example.com", "type": "AAAA", "ttl": 60, "data": "::1"},
    ]}


def test_parse_mx_with_compression_and_txt():
    mx = struct.pack(">H", 10) + b"\x04mail\xc0\x0c"
    txt = b"\x05hello\x05world"
    data = response(9, 0x8180, 15, [(15, 60, mx), (16, 30, txt)])
    answers = parse_answer(data, 9)["answers"]
    assert answers[0]["data"] == "10 mail.example.com"
    assert answers[1] == {"name": "example.com", "type": "TXT", "ttl": 30, "data": "helloworld"}


def test_parse_cname_and_ns():
    data = response(3, 0x8180, 5, [(5, 10, b"\x03www\xc0\x0c"), (2, 10, b"\x02ns\xc0\x0c")])
    assert [a["data"] for a in parse_answer(data, 3)["answers"]] == ["www.example.com", "ns.example.com"]


def test_nxdomain():
    assert parse_answer(response(4, 0x8183, 1, []), 4) == {"rcode": "NXDOMAIN", "answers": []}


def test_rejects_wrong_id_short_and_loops():
    with pytest.raises(ValueError):
        parse_answer(response(4, 0x8180, 1, []), 5)
    with pytest.raises(ValueError):
        parse_answer(b"\x00\x01", 1)
    with pytest.raises(ValueError):
        read_name(b"\xc0\x00", 0)
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/pytest tests/test_mcp_dnsq.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.mcp.dnsq'`

- [ ] **Step 3: Implement**

`ethtester/mcp/dnsq.py`:
```python
"""DNS lookups of common record types, sent only through the tested interface."""

import random
import socket
import struct
import time

from ..checks.dns import SO_BINDTODEVICE
from .validate import RECORD_TYPES

TYPE_NAMES = {number: name for name, number in RECORD_TYPES.items()}
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
MAX_POINTER_HOPS = 20


def build_query(name: str, qtype: int, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    labels = name.strip(".").split(".")
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"
    return header + qname + struct.pack(">HH", qtype, 1)


def read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decodes a possibly compressed name; returns it and the offset just after it."""
    labels: list[str] = []
    end = None
    hops = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            hops += 1
            if hops > MAX_POINTER_HOPS:
                raise ValueError("DNS name compression loop")
            continue
        if length == 0:
            return ".".join(labels), (end if end is not None else offset + 1)
        labels.append(data[offset + 1:offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length


def _rdata(data: bytes, rtype: int, start: int, length: int) -> str:
    raw = data[start:start + length]
    if rtype == 1 and length == 4:
        return socket.inet_ntop(socket.AF_INET, raw)
    if rtype == 28 and length == 16:
        return socket.inet_ntop(socket.AF_INET6, raw)
    if rtype in (2, 5):
        return read_name(data, start)[0]
    if rtype == 15:
        preference = struct.unpack(">H", raw[:2])[0]
        return f"{preference} {read_name(data, start + 2)[0]}"
    if rtype == 16:
        parts, i = [], 0
        while i < len(raw):
            size = raw[i]
            parts.append(raw[i + 1:i + 1 + size].decode("utf-8", "replace"))
            i += 1 + size
        return "".join(parts)
    return raw.hex()


def parse_answer(data: bytes, qid: int) -> dict:
    if len(data) < 12:
        raise ValueError("short DNS response")
    rid, flags, questions, answers, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != qid or not flags & 0x8000:
        raise ValueError("unexpected DNS response")
    offset = 12
    for _ in range(questions):
        _, offset = read_name(data, offset)
        offset += 4
    records = []
    for _ in range(answers):
        name, offset = read_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS record")
        rtype, _cls, ttl, length = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        records.append({"name": name, "type": TYPE_NAMES.get(rtype, str(rtype)), "ttl": ttl,
                        "data": _rdata(data, rtype, offset, length)})
        offset += length
    return {"rcode": RCODES.get(flags & 0x000F, str(flags & 0x000F)), "answers": records}


def lookup(server: str, name: str, qtype: str, interface: str, timeout: float = 3.0,
           clock=time.monotonic) -> dict:
    qid = random.randrange(65536)
    started = clock()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode())
        sock.settimeout(timeout)
        sock.sendto(build_query(name, RECORD_TYPES[qtype], qid), (server, 53))
        data, _ = sock.recvfrom(4096)
    result = parse_answer(data, qid)
    result.update(server=server, ms=round((clock() - started) * 1000, 1))
    return result
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/pytest tests/test_mcp_dnsq.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add ethtester/mcp/dnsq.py tests/test_mcp_dnsq.py
git commit -m "feat(mcp): DNS lookups of A/AAAA/CNAME/MX/NS/TXT bound to the interface"
```

---

### Task 4: Probe commands and parsers

**Files:**
- Create: `ethtester/mcp/probes.py`
- Test: `tests/test_mcp_probes.py`

**Interfaces:**
- Consumes: `run_command` and `parse_nmcli` (existing).
- Produces (each runner takes `run=run_command` and returns a JSON-able dict):
  - Ping: `parse_ping(raw)` and `ping(interface, host, count, size, run)`.
  - Traceroute: `parse_traceroute(raw) -> list` and `traceroute(interface, host, max_hops, run)`.
  - ethtool: `parse_ethtool(raw) -> dict`, with keys `speed`, `duplex`, `autoneg` (bool), `link_detected` (bool), `supported_modes`, `advertised_modes` and `partner_modes`. Also `parse_ethtool_stats(raw) -> dict[str, int]`.
  - HTTP: `parse_curl(raw)` and `http_check(interface, url, run)`.
  - DHCP: `dhcp_renew(interface, run, clock=time.monotonic, sleep=time.sleep, timeout=30.0)`.
  - Neighbours: `parse_arp_scan(raw) -> list` and `neighbour_scan(interface, run)`.
  - Speed test: `parse_iperf(raw)` and `speed_test(interface, server, seconds, reverse, run)`.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_probes.py`:
```python
import json

from ethtester.mcp import probes

PING = """PING 192.168.1.1 (192.168.1.1) from 192.168.1.222 eth0: 56(84) bytes of data.

--- 192.168.1.1 ping statistics ---
5 packets transmitted, 4 received, 20% packet loss, time 812ms
rtt min/avg/max/mdev = 0.981/1.356/2.004/0.310 ms
"""

TRACEROUTE = """traceroute to 1.1.1.1 (1.1.1.1), 20 hops max, 60 byte packets
 1  192.168.1.1  0.981 ms
 2  *
 3  10.0.0.1  5.123 ms
"""

ETHTOOL = """Settings for eth0:
\tSupported ports: [ TP MII ]
\tSupported link modes:   10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\tAdvertised link modes:  10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\tLink partner advertised link modes:  10baseT/Half 10baseT/Full
\t                                     100baseT/Full 1000baseT/Full
\tSpeed: 100Mb/s
\tDuplex: Full
\tAuto-negotiation: on
\tLink detected: yes
"""

STATS = """NIC statistics:
     tx_packets: 1234
     rx_errors: 3
     rx_missed: 0
"""

CURL = """*   Trying 1.1.1.1:443...
*  expire date: Jan 15 12:00:00 2027 GMT
< HTTP/2 200

ETHTESTER_JSON {"http_code":200,"exitcode":0,"errormsg":null,"remote_ip":"1.1.1.1","time_namelookup":0.001,"time_connect":0.012,"time_appconnect":0.045,"time_starttransfer":0.080,"time_total":0.081}
"""

ARP = "192.168.1.1\t60:38:e0:aa:bb:cc\tBelkin International Inc.\n192.168.1.20\t00:e0:4c:36:00:f5\t(Unknown)\n"

IPERF = json.dumps({"end": {"sum_received": {"bits_per_second": 93_400_000.0},
                            "sum_sent": {"retransmits": 4}}})


class Recorder:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.outputs.pop(0) if self.outputs else ""


def test_ping():
    run = Recorder(PING)
    result = probes.ping("eth0", "192.168.1.1", 5, 56, run=run)
    assert result == {"host": "192.168.1.1", "sent": 5, "received": 4, "loss_pct": 20.0,
                      "rtt_ms": {"min": 0.981, "avg": 1.356, "max": 2.004}}
    cmd, kwargs = run.calls[0]
    assert cmd == ["ping", "-I", "eth0", "-n", "-c", "5", "-s", "56", "-W", "2", "-i", "0.2", "192.168.1.1"]
    assert kwargs["check"] is False


def test_ping_total_loss():
    assert probes.parse_ping("3 packets transmitted, 0 received, 100% packet loss")["rtt_ms"] is None


def test_traceroute():
    run = Recorder(TRACEROUTE)
    result = probes.traceroute("eth0", "1.1.1.1", 20, run=run)
    assert result["hops"] == [{"hop": 1, "ip": "192.168.1.1", "rtt_ms": 0.981},
                              {"hop": 2, "ip": None, "rtt_ms": None},
                              {"hop": 3, "ip": "10.0.0.1", "rtt_ms": 5.123}]
    assert run.calls[0][0] == ["traceroute", "-i", "eth0", "-n", "-q", "1", "-w", "2", "-m", "20", "1.1.1.1"]


def test_ethtool():
    parsed = probes.parse_ethtool(ETHTOOL)
    assert parsed["speed"] == "100Mb/s"
    assert parsed["duplex"] == "Full"
    assert parsed["autoneg"] is True and parsed["link_detected"] is True
    assert parsed["advertised_modes"] == ["10baseT/Half", "10baseT/Full", "100baseT/Half", "100baseT/Full"]
    assert "1000baseT/Full" in parsed["partner_modes"]
    assert probes.parse_ethtool("Settings for eth0:\n\tLink partner advertised link modes:  Not reported\n")["partner_modes"] == []


def test_ethtool_stats():
    assert probes.parse_ethtool_stats(STATS) == {"tx_packets": 1234, "rx_errors": 3, "rx_missed": 0}


def test_http_check():
    run = Recorder(CURL)
    result = probes.http_check("eth0", "https://1.1.1.1/", run=run)
    assert result["ok"] is True and result["status"] == 200
    assert result["ms"] == {"dns": 1.0, "connect": 12.0, "tls": 45.0, "first_byte": 80.0, "total": 81.0}
    assert result["cert_expires"] == "Jan 15 12:00:00 2027 GMT"
    cmd = run.calls[0][0]
    assert cmd[:3] == ["curl", "--interface", "eth0"] and cmd[-1] == "https://1.1.1.1/"


def test_http_check_failure():
    raw = 'curl: (6) Could not resolve host\nETHTESTER_JSON {"http_code":0,"exitcode":6,"errormsg":"Could not resolve host: x"}\n'
    result = probes.parse_curl(raw)
    assert result["ok"] is False and result["status"] is None and "resolve" in result["error"]
    assert probes.parse_curl("")["ok"] is False


def test_dhcp_renew():
    lease = "IP4.ADDRESS[1]:192.168.1.222/24\nIP4.GATEWAY:192.168.1.1\n"
    run = Recorder("", "", "", lease)
    times = iter([0.0, 0.0, 1.0, 2.5, 2.5])
    result = probes.dhcp_renew("eth0", run=run, clock=lambda: next(times), sleep=lambda s: None)
    assert result["ok"] is True and result["ip"] == "192.168.1.222" and result["seconds_to_ip"] == 2.5
    assert run.calls[0][0] == ["nmcli", "--wait", "10", "device", "disconnect", "eth0"]
    assert run.calls[1][0] == ["nmcli", "--wait", "0", "device", "connect", "eth0"]


def test_dhcp_renew_timeout():
    times = iter([0.0, 0.0, 31.0])
    result = probes.dhcp_renew("eth0", run=Recorder(), clock=lambda: next(times), sleep=lambda s: None)
    assert result["ok"] is False and "30" in result["error"]


def test_neighbour_scan():
    run = Recorder(ARP)
    result = probes.neighbour_scan("eth0", run=run)
    assert result["neighbours"][0] == {"ip": "192.168.1.1", "mac": "60:38:e0:aa:bb:cc",
                                       "vendor": "Belkin International Inc."}
    assert run.calls[0][0][:3] == ["arp-scan", "--interface", "eth0"]


def test_speed_test():
    run = Recorder(IPERF)
    result = probes.speed_test("eth0", "mac.lan", 10, True, run=run)
    assert result == {"server": "mac.lan", "reverse": True, "ok": True, "mbps": 93.4, "retransmits": 4}
    assert run.calls[0][0] == ["iperf3", "-c", "mac.lan", "--bind-dev", "eth0", "-t", "10", "-J", "-R"]
    assert probes.parse_iperf('{"error": "unable to connect"}') == {"ok": False, "error": "unable to connect"}
    assert probes.parse_iperf("garbage")["ok"] is False
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/pytest tests/test_mcp_probes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.mcp.probes'`

- [ ] **Step 3: Implement**

`ethtester/mcp/probes.py`:
```python
"""Probe commands and their output parsers. Commands are argv lists, never shell strings."""

import json
import re
import time
from dataclasses import asdict

from ..checks.command import run_command
from ..checks.dhcp import parse_nmcli

PING_COUNTS_RE = re.compile(r"(\d+) packets transmitted, (\d+) received")
PING_LOSS_RE = re.compile(r"([\d.]+)% packet loss")
PING_RTT_RE = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/")
HOP_RE = re.compile(r"^\s*(\d+)\s+(\S+)(?:\s+([\d.]+) ms)?")
EXPIRE_RE = re.compile(r"expire date:\s*(.+)$", re.IGNORECASE)
JSON_MARK = "ETHTESTER_JSON "
MODE_KEYS = {"Supported link modes": "supported_modes",
             "Advertised link modes": "advertised_modes",
             "Link partner advertised link modes": "partner_modes"}


def parse_ping(raw: str) -> dict:
    counts = PING_COUNTS_RE.search(raw)
    loss = PING_LOSS_RE.search(raw)
    rtt = PING_RTT_RE.search(raw)
    return {
        "sent": int(counts.group(1)) if counts else 0,
        "received": int(counts.group(2)) if counts else 0,
        "loss_pct": float(loss.group(1)) if loss else 100.0,
        "rtt_ms": ({"min": float(rtt.group(1)), "avg": float(rtt.group(2)), "max": float(rtt.group(3))}
                   if rtt else None),
    }


def ping(interface: str, host: str, count: int, size: int, run=run_command) -> dict:
    cmd = ["ping", "-I", interface, "-n", "-c", str(count), "-s", str(size), "-W", "2", "-i", "0.2", host]
    return {"host": host, **parse_ping(run(cmd, timeout=count * 2.5 + 5, check=False))}


def parse_traceroute(raw: str) -> list:
    hops = []
    for line in raw.splitlines()[1:]:
        m = HOP_RE.match(line)
        if m:
            hops.append({"hop": int(m.group(1)),
                         "ip": None if m.group(2) == "*" else m.group(2),
                         "rtt_ms": float(m.group(3)) if m.group(3) else None})
    return hops


def traceroute(interface: str, host: str, max_hops: int, run=run_command) -> dict:
    cmd = ["traceroute", "-i", interface, "-n", "-q", "1", "-w", "2", "-m", str(max_hops), host]
    return {"host": host, "hops": parse_traceroute(run(cmd, timeout=max_hops * 2 + 10, check=False))}


def parse_ethtool(raw: str) -> dict:
    fields: dict[str, str] = {}
    key = None
    for line in raw.splitlines()[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
        elif key is not None:
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    result = {
        "speed": fields.get("Speed"),
        "duplex": fields.get("Duplex"),
        "autoneg": fields.get("Auto-negotiation") == "on",
        "link_detected": fields.get("Link detected") == "yes",
    }
    for source, target in MODE_KEYS.items():
        value = fields.get(source, "")
        result[target] = [] if value in ("", "Not reported") else value.split()
    return result


def parse_ethtool_stats(raw: str) -> dict:
    counters = {}
    for line in raw.splitlines():
        name, sep, value = line.strip().partition(":")
        value = value.strip()
        if sep and value.lstrip("-").isdigit():
            counters[name.strip()] = int(value)
    return counters


def parse_curl(raw: str) -> dict:
    data: dict = {}
    expires = None
    for line in raw.splitlines():
        if line.startswith(JSON_MARK):
            data = json.loads(line[len(JSON_MARK):])
        else:
            m = EXPIRE_RE.search(line)
            if m:
                expires = m.group(1).strip()

    def ms(key):
        value = data.get(key)
        return round(value * 1000, 1) if isinstance(value, (int, float)) else None

    return {
        "ok": data.get("exitcode", 1) == 0,
        "status": data.get("http_code") or None,
        "error": data.get("errormsg") or (None if data else "no result from curl"),
        "remote_ip": data.get("remote_ip") or None,
        "ms": {"dns": ms("time_namelookup"), "connect": ms("time_connect"), "tls": ms("time_appconnect"),
               "first_byte": ms("time_starttransfer"), "total": ms("time_total")},
        "cert_expires": expires,
    }


def http_check(interface: str, url: str, run=run_command) -> dict:
    cmd = ["curl", "--interface", interface, "-sS", "-v", "--stderr", "-", "-o", "/dev/null",
           "-m", "15", "-w", "\n" + JSON_MARK + "%{json}\n", url]
    return {"url": url, **parse_curl(run(cmd, timeout=20, check=False))}


def dhcp_renew(interface: str, run=run_command, clock=time.monotonic, sleep=time.sleep,
               timeout: float = 30.0) -> dict:
    """Drops the lease and reconnects; the main tester briefly shows the port without an address."""
    run(["nmcli", "--wait", "10", "device", "disconnect", interface], timeout=15, check=False)
    started = clock()
    run(["nmcli", "--wait", "0", "device", "connect", interface], check=False)
    while clock() - started < timeout:
        lease = parse_nmcli(run(["nmcli", "-t", "-f", "IP4,DHCP4", "device", "show", interface], check=False))
        if lease.ip:
            return {"ok": True, "seconds_to_ip": round(clock() - started, 1), **asdict(lease)}
        sleep(0.5)
    return {"ok": False, "seconds_to_ip": None, "error": f"no DHCP lease within {timeout:.0f} s"}


def parse_arp_scan(raw: str) -> list:
    found = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].count(".") == 3:
            found.append({"ip": parts[0], "mac": parts[1], "vendor": parts[2] if len(parts) > 2 else None})
    return found


def neighbour_scan(interface: str, run=run_command) -> dict:
    cmd = ["arp-scan", "--interface", interface, "--localnet", "--plain", "--retry", "2"]
    return {"neighbours": parse_arp_scan(run(cmd, timeout=90, check=False))}


def parse_iperf(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "iperf3 gave no JSON output"}
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    end = data.get("end", {})
    bps = end.get("sum_received", {}).get("bits_per_second")
    return {"ok": bps is not None, "mbps": round(bps / 1e6, 1) if bps is not None else None,
            "retransmits": end.get("sum_sent", {}).get("retransmits")}


def speed_test(interface: str, server: str, seconds: int, reverse: bool, run=run_command) -> dict:
    cmd = ["iperf3", "-c", server, "--bind-dev", interface, "-t", str(seconds), "-J"] + (["-R"] if reverse else [])
    return {"server": server, "reverse": reverse, **parse_iperf(run(cmd, timeout=seconds + 20, check=False))}
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/pytest tests/test_mcp_probes.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add ethtester/mcp/probes.py tests/test_mcp_probes.py
git commit -m "feat(mcp): probe commands and output parsers"
```

---

### Task 5: Link control with automatic revert

**Files:**
- Create: `ethtester/mcp/linkctl.py`
- Test: `tests/test_mcp_linkctl.py`

**Interfaces:**
- Consumes: `run_command` and `parse_ethtool` (Task 4).
- Produces:
  - `Busy(Exception)`.
  - `LinkControl(interface, run=run_command, timer=threading.Timer, clock=time.monotonic)` with these methods:
    - `exclusive()`, a context manager that raises `Busy` if the lock is held;
    - `revert_in() -> float | None`;
    - `renegotiate() -> dict`;
    - `force(speed: int, duration_s: int) -> dict`;
    - `restore_autoneg() -> None`, which only acts when autoneg is off;
    - `recover() -> None`, used at server start: it restores autoneg and reconnects the device if NetworkManager reports it as `disconnected`.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_linkctl.py`:
```python
import pytest

from ethtester.mcp.linkctl import Busy, LinkControl

AUTONEG_ON = "Settings for eth0:\n\tSpeed: 100Mb/s\n\tAuto-negotiation: on\n"
AUTONEG_OFF = "Settings for eth0:\n\tSpeed: 10Mb/s\n\tAuto-negotiation: off\n"


class FakeRun:
    def __init__(self, ethtool=AUTONEG_ON, devices="eth0:connected\n"):
        self.ethtool = ethtool
        self.devices = devices
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd == ["ethtool", "eth0"]:
            return self.ethtool
        if cmd[:4] == ["nmcli", "-t", "-f", "DEVICE,STATE"]:
            return self.devices
        return ""


class FakeTimer:
    created = []

    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn
        FakeTimer.created.append(self)

    def start(self):
        pass


def make(run=None):
    FakeTimer.created = []
    run = run or FakeRun()
    return run, LinkControl("eth0", run=run, timer=FakeTimer, clock=lambda: 100.0)


def test_renegotiate():
    run, ctl = make()
    assert ctl.renegotiate() == {"action": "renegotiate"}
    assert run.calls == [["ethtool", "-r", "eth0"]]


def test_force_holds_lock_until_revert():
    run, ctl = make()
    assert ctl.force(10, 60) == {"action": "force_10", "reverts_in_s": 60}
    assert run.calls[0] == ["ethtool", "-s", "eth0", "speed", "10", "duplex", "full", "autoneg", "off"]
    assert ctl.revert_in() == 60.0
    with pytest.raises(Busy, match="reverts in 60"):
        ctl.renegotiate()
    with pytest.raises(Busy):
        with ctl.exclusive():
            pass
    run.ethtool = AUTONEG_OFF
    timer = FakeTimer.created[0]
    assert timer.delay == 60 and timer.daemon is True
    timer.fn()
    assert run.calls[-1] == ["ethtool", "-s", "eth0", "autoneg", "on"]
    assert ctl.revert_in() is None
    with ctl.exclusive():
        pass


def test_force_failure_releases_lock():
    class Failing(FakeRun):
        def __call__(self, cmd, **kwargs):
            if cmd[:2] == ["ethtool", "-s"]:
                raise RuntimeError("ethtool failed")
            return super().__call__(cmd, **kwargs)

    _, ctl = make(Failing())
    with pytest.raises(RuntimeError):
        ctl.force(100, 30)
    with ctl.exclusive():
        pass


def test_restore_only_when_autoneg_off():
    run, ctl = make()
    ctl.restore_autoneg()
    assert ["ethtool", "-s", "eth0", "autoneg", "on"] not in run.calls
    run.ethtool = AUTONEG_OFF
    ctl.restore_autoneg()
    assert run.calls[-1] == ["ethtool", "-s", "eth0", "autoneg", "on"]


def test_recover_reconnects_disconnected_device():
    run, ctl = make(FakeRun(ethtool=AUTONEG_ON, devices="wlan0:connected\neth0:disconnected\n"))
    ctl.recover()
    assert run.calls[-1] == ["nmcli", "--wait", "0", "device", "connect", "eth0"]
    run2, ctl2 = make(FakeRun())
    ctl2.recover()
    assert ["nmcli", "--wait", "0", "device", "connect", "eth0"] not in run2.calls
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/pytest tests/test_mcp_linkctl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.mcp.linkctl'`

- [ ] **Step 3: Implement**

`ethtester/mcp/linkctl.py`:
```python
"""Link changes for diagnosis: renegotiate or force a speed, always reverting to autoneg."""

import logging
import threading
import time
from contextlib import contextmanager

from ..checks.command import run_command
from .probes import parse_ethtool

log = logging.getLogger(__name__)


class Busy(Exception):
    pass


class LinkControl:
    def __init__(self, interface: str, run=run_command, timer=threading.Timer, clock=time.monotonic):
        self.interface = interface
        self.run = run
        self.timer = timer
        self.clock = clock
        self.lock = threading.Lock()
        self._revert_at: float | None = None

    def _busy_message(self) -> str:
        left = self.revert_in()
        if left is not None:
            return f"link is forced; reverts in {left:.0f} s"
        return "another link or DHCP action is running"

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise Busy(self._busy_message())
        try:
            yield
        finally:
            self.lock.release()

    def revert_in(self) -> float | None:
        return None if self._revert_at is None else max(0.0, self._revert_at - self.clock())

    def renegotiate(self) -> dict:
        with self.exclusive():
            self.run(["ethtool", "-r", self.interface])
        return {"action": "renegotiate"}

    def force(self, speed: int, duration_s: int) -> dict:
        """Forces a speed and keeps the lock until the timer has restored autoneg."""
        if not self.lock.acquire(blocking=False):
            raise Busy(self._busy_message())
        try:
            self.run(["ethtool", "-s", self.interface, "speed", str(speed), "duplex", "full", "autoneg", "off"])
        except Exception:
            self.lock.release()
            raise
        self._revert_at = self.clock() + duration_s
        timer = self.timer(duration_s, self._revert)
        timer.daemon = True
        timer.start()
        return {"action": f"force_{speed}", "reverts_in_s": duration_s}

    def _revert(self) -> None:
        try:
            self.restore_autoneg()
        except Exception:
            log.exception("autoneg revert failed")
        finally:
            self._revert_at = None
            self.lock.release()

    def restore_autoneg(self) -> None:
        """Turns autoneg back on only when it is off, so a healthy link doesn't bounce."""
        settings = parse_ethtool(self.run(["ethtool", self.interface], check=False))
        if settings["speed"] is not None and not settings["autoneg"]:
            self.run(["ethtool", "-s", self.interface, "autoneg", "on"])

    def recover(self) -> None:
        """Undoes anything a crashed previous run may have left: forced speed, disconnected device."""
        self.restore_autoneg()
        states = self.run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], check=False)
        for line in states.splitlines():
            device, _, state = line.partition(":")
            if device == self.interface and state == "disconnected":
                self.run(["nmcli", "--wait", "0", "device", "connect", self.interface], check=False)
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/pytest tests/test_mcp_linkctl.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add ethtester/mcp/linkctl.py tests/test_mcp_linkctl.py
git commit -m "feat(mcp): link control with lock and automatic autoneg revert"
```

---

### Task 6: Probe log and ProbeService

**Files:**
- Create: `ethtester/mcp/probelog.py`, `ethtester/mcp/service.py`
- Test: `tests/test_mcp_service.py`

**Interfaces:**
- Consumes:
  - `Config` (Task 1 fields);
  - `check_link` and `check_dhcp` (existing);
  - `validate`, `dnsq.lookup` and `probes` (Tasks 2–4);
  - `LinkControl` and `Busy` (Task 5).
- Produces:
  - `ProbeLog(path).record(tool, args, duration_s, ok, error=None, wall_time=None)`.
  - `ProbeError(Exception)`.
  - `ProbeService(cfg, linkctl, probelog=None, run=run_command, link=check_link, dhcp=check_dhcp, dns=dnsq.lookup, sleep=time.sleep, wall=time.time)` with these methods:
    - status: `get_status()`, `get_history(limit=50, since=None)`, `get_link_details()`;
    - reachability: `ping(host, count=5, size=56)`, `traceroute(host, max_hops=20)`, `dns_lookup(name, record_type="A", server=None)`, `http_check(url)`;
    - disruptive: `dhcp_renew()`, `link_control(action, duration_s=60)`, `neighbour_scan()`;
    - throughput: `speed_test(server, seconds=10, reverse=False)`.
  - Every method returns a dict and logs one `ProbeLog` line. A bad argument raises `ValueError`, and a precondition failure raises `ProbeError` or `Busy`.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_service.py`:
```python
import json
from pathlib import Path

import pytest

from ethtester.config import Config
from ethtester.mcp.linkctl import Busy, LinkControl
from ethtester.mcp.probelog import ProbeLog
from ethtester.mcp.service import ProbeError, ProbeService
from ethtester.results import DhcpResult, LinkResult
from ethtester.statefile import write_state
from ethtester.verdict import evaluate
from tests.factories import ok_snapshot

PING = "5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 1.0/1.5/2.0/0.1 ms\n"
LEASE = DhcpResult(ip="192.168.1.222", prefix=24, gateway="192.168.1.1", dns_servers=("192.168.1.1",))


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "ping":
            return PING
        if cmd == ["ethtool", "eth0"]:
            return "Settings for eth0:\n\tSpeed: 100Mb/s\n\tAuto-negotiation: on\n"
        return ""


class NoTimer:
    def __init__(self, delay, fn):
        pass

    def start(self):
        pass


def make(tmp_path: Path, up=True, lease=LEASE):
    cfg = Config(state_path=str(tmp_path / "state.json"), log_path=str(tmp_path / "ethtester.log"))
    run = FakeRun()
    linkctl = LinkControl("eth0", run=run, timer=NoTimer, clock=lambda: 0.0)
    probelog = ProbeLog(tmp_path / "probes.log")
    dns_calls = []

    def dns(server, name, qtype, interface):
        dns_calls.append((server, name, qtype, interface))
        return {"rcode": "NOERROR", "answers": [], "server": server, "ms": 1.0}

    service = ProbeService(cfg, linkctl, probelog=probelog, run=run,
                           link=lambda i: LinkResult(up=up, speed_mbps=100 if up else None, carrier_changes=2),
                           dhcp=lambda i: lease, dns=dns, sleep=lambda s: None, wall=lambda: 1_000.0)
    return service, run, dns_calls, tmp_path / "probes.log"


def test_get_status_fresh_and_stale(tmp_path):
    service, *_ = make(tmp_path)
    s = ok_snapshot()
    write_state(tmp_path / "state.json", s, evaluate(s, 1000, 20), 990.0)
    status = service.get_status()
    assert status["verdict"] == "OK" and status["age_s"] == 10.0 and status["stale"] is False
    write_state(tmp_path / "state.json", s, evaluate(s, 1000, 20), 900.0)
    assert service.get_status()["stale"] is True


def test_get_status_missing(tmp_path):
    service, *_ = make(tmp_path)
    with pytest.raises(ProbeError, match="no status"):
        service.get_status()


def test_get_history(tmp_path):
    service, *_ = make(tmp_path)
    lines = [{"ts": f"2026-10-21T09:4{i}:00", "verdict": "OK", "text": "OK",
              "snapshot": json.loads(json.dumps({"link": {"up": True, "speed_mbps": 100},
                                                 "dhcp": {"ip": "10.0.0.2"}, "lldp": None}))}
             for i in range(5)]
    (tmp_path / "ethtester.log").write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
    entries = service.get_history(limit=2)["entries"]
    assert [e["ts"] for e in entries] == ["2026-10-21T09:43:00", "2026-10-21T09:44:00"]
    assert entries[0] == {"ts": "2026-10-21T09:43:00", "verdict": "OK", "text": "OK", "link_up": True,
                          "speed_mbps": 100, "ip": "10.0.0.2", "switch": None, "port": None}
    assert len(service.get_history(since="2026-10-21T09:42:00")["entries"]) == 3
    with pytest.raises(ValueError):
        service.get_history(limit=500)


def test_ping_validates_checks_link_and_logs(tmp_path):
    service, run, _, log_path = make(tmp_path)
    result = service.ping("192.168.1.1", count=5)
    assert result["received"] == 5
    assert run.calls[-1][:3] == ["ping", "-I", "eth0"]
    with pytest.raises(ValueError):
        service.ping("-f 192.168.1.1")
    entries = [json.loads(l) for l in log_path.read_text().splitlines()]
    assert [(e["tool"], e["ok"]) for e in entries] == [("ping", True), ("ping", False)]
    assert entries[0]["args"] == {"host": "192.168.1.1", "count": 5, "size": 56}


def test_probe_refuses_without_link(tmp_path):
    service, *_ = make(tmp_path, up=False)
    with pytest.raises(ProbeError, match="no link"):
        service.ping("1.1.1.1")


def test_dns_lookup_defaults_to_dhcp_server(tmp_path):
    service, _, dns_calls, _ = make(tmp_path)
    service.dns_lookup("example.com", record_type="mx")
    assert dns_calls == [("192.168.1.1", "example.com", "MX", "eth0")]
    service.dns_lookup("example.com", server="1.1.1.1")
    assert dns_calls[-1][0] == "1.1.1.1"
    with pytest.raises(ValueError):
        service.dns_lookup("example.com", server="dns.google")


def test_neighbour_scan_refuses_large_subnet(tmp_path):
    service, *_ = make(tmp_path, lease=DhcpResult(ip="10.1.2.3", prefix=16))
    with pytest.raises(ProbeError, match="/16"):
        service.neighbour_scan()


def test_link_control_force_then_busy(tmp_path):
    service, run, _, _ = make(tmp_path)
    result = service.link_control("force_10", duration_s=30)
    assert result["action"] == "force_10" and result["link"]["up"] is True
    with pytest.raises(Busy):
        service.dhcp_renew()
    with pytest.raises(ValueError):
        service.link_control("force_1000")
    with pytest.raises(ValueError):
        service.link_control("force_10", duration_s=5)


def test_speed_test_validates(tmp_path):
    service, run, _, _ = make(tmp_path)
    service.speed_test("mac.lan", seconds=5)
    assert run.calls[-1][:2] == ["iperf3", "-c"]
    with pytest.raises(ValueError):
        service.speed_test("mac.lan", reverse="yes")
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/pytest tests/test_mcp_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.mcp.probelog'`

- [ ] **Step 3: Implement**

`ethtester/mcp/probelog.py`:
```python
"""One JSON line per MCP tool call, so the user can see what Claude ran."""

import json
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class ProbeLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.getLogger(f"ethtester.probes.{path}")
        self.logger.handlers[:] = [handler]
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def record(self, tool: str, args: dict, duration_s: float, ok: bool, error: str | None = None,
               wall_time: float | None = None) -> None:
        when = datetime.fromtimestamp(wall_time if wall_time is not None else time.time())
        self.logger.info(json.dumps({"ts": when.isoformat(timespec="seconds"), "tool": tool, "args": args,
                                     "duration_s": duration_s, "ok": ok, "error": error}))
```

`ethtester/mcp/service.py`:
```python
"""The operations behind the MCP tools: validate, check the link, run the probe, log the call."""

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..checks.command import run_command
from ..checks.dhcp import check_dhcp
from ..checks.link import check_link
from ..config import Config
from . import dnsq, probes, validate

log = logging.getLogger(__name__)

STALE_S = 30
LINK_SETTLE_S = 3.0
MAX_SCAN_PREFIX = 22
FORCE_SPEEDS = {"force_10": 10, "force_100": 100}


class ProbeError(Exception):
    pass


def _summarise(entry: dict) -> dict:
    snap = entry.get("snapshot") or {}
    link = snap.get("link") or {}
    dhcp = snap.get("dhcp") or {}
    lldp = snap.get("lldp") or {}
    return {"ts": entry.get("ts"), "verdict": entry.get("verdict"), "text": entry.get("text"),
            "link_up": link.get("up"), "speed_mbps": link.get("speed_mbps"), "ip": dhcp.get("ip"),
            "switch": lldp.get("switch_name"), "port": lldp.get("port")}


class ProbeService:
    def __init__(self, cfg: Config, linkctl, probelog=None, run=run_command, link=check_link,
                 dhcp=check_dhcp, dns=dnsq.lookup, sleep=time.sleep, wall=time.time):
        self.cfg = cfg
        self.iface = cfg.interface
        self.linkctl = linkctl
        self.probelog = probelog
        self.run = run
        self.link = link
        self.dhcp = dhcp
        self.dns = dns
        self.sleep = sleep
        self.wall = wall

    def _logged(self, tool: str, args: dict, fn):
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            self._log(tool, args, started, False, str(exc))
            raise
        self._log(tool, args, started, True, None)
        return result

    def _log(self, tool, args, started, ok, error):
        if self.probelog is None:
            return
        try:
            self.probelog.record(tool, args, round(time.monotonic() - started, 2), ok, error, self.wall())
        except Exception:
            log.exception("probe log write failed")

    def _require_link(self):
        link = self.link(self.iface)
        if not link.up:
            raise ProbeError(f"{self.iface} has no link; plug in a cable first")
        return link

    def get_status(self) -> dict:
        def body():
            try:
                data = json.loads(Path(self.cfg.state_path).read_text())
            except FileNotFoundError:
                raise ProbeError("no status yet; is the ethtester service running?") from None
            age = self.wall() - data.get("epoch", 0)
            return {**data, "age_s": round(age, 1), "stale": age > STALE_S}
        return self._logged("get_status", {}, body)

    def get_history(self, limit: int = 50, since: str | None = None) -> dict:
        args = {"limit": limit, "since": since}

        def body():
            count = validate.bounded("limit", limit, 1, 200)
            start = validate.timestamp(since) if since else None
            path = Path(self.cfg.log_path)
            rows = []
            for name in (path.with_name(path.name + ".1"), path):
                if not name.exists():
                    continue
                for line in name.read_text().splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if start is not None and datetime.fromisoformat(entry["ts"]) < start:
                        continue
                    rows.append(_summarise(entry))
            return {"entries": rows[-count:]}
        return self._logged("get_history", args, body)

    def get_link_details(self) -> dict:
        def body():
            settings = probes.parse_ethtool(self.run(["ethtool", self.iface]))
            counters = probes.parse_ethtool_stats(self.run(["ethtool", "-S", self.iface], check=False))
            link = self.link(self.iface)
            return {"interface": self.iface, **settings, "carrier_changes": link.carrier_changes,
                    "counters": counters, "forced_reverts_in_s": self.linkctl.revert_in()}
        return self._logged("get_link_details", {}, body)

    def ping(self, host: str, count: int = 5, size: int = 56) -> dict:
        args = {"host": host, "count": count, "size": size}

        def body():
            target = validate.host(host)
            n = validate.bounded("count", count, 1, 20)
            s = validate.bounded("size", size, 16, 1472)
            self._require_link()
            return probes.ping(self.iface, target, n, s, run=self.run)
        return self._logged("ping", args, body)

    def traceroute(self, host: str, max_hops: int = 20) -> dict:
        args = {"host": host, "max_hops": max_hops}

        def body():
            target = validate.host(host)
            hops = validate.bounded("max_hops", max_hops, 1, 30)
            self._require_link()
            return probes.traceroute(self.iface, target, hops, run=self.run)
        return self._logged("traceroute", args, body)

    def dns_lookup(self, name: str, record_type: str = "A", server: str | None = None) -> dict:
        args = {"name": name, "record_type": record_type, "server": server}

        def body():
            target = validate.host(name)
            qtype = validate.record_type(record_type)
            if server is not None:
                resolver = validate.ipv4(server)
            else:
                lease = self.dhcp(self.iface)
                candidates = lease.dns_servers or ((lease.gateway,) if lease.gateway else ())
                if not candidates:
                    raise ProbeError("no DNS server from DHCP; pass server explicitly")
                resolver = candidates[0]
            self._require_link()
            try:
                return self.dns(resolver, target, qtype, self.iface)
            except OSError as exc:
                raise ProbeError(f"no answer from {resolver}: {exc}") from None
        return self._logged("dns_lookup", args, body)

    def http_check(self, url: str) -> dict:
        def body():
            target = validate.url(url)
            self._require_link()
            return probes.http_check(self.iface, target, run=self.run)
        return self._logged("http_check", {"url": url}, body)

    def dhcp_renew(self) -> dict:
        def body():
            with self.linkctl.exclusive():
                self._require_link()
                return probes.dhcp_renew(self.iface, run=self.run, sleep=self.sleep)
        return self._logged("dhcp_renew", {}, body)

    def link_control(self, action: str, duration_s: int = 60) -> dict:
        args = {"action": action, "duration_s": duration_s}

        def body():
            if action == "renegotiate":
                result = self.linkctl.renegotiate()
            elif action in FORCE_SPEEDS:
                seconds = validate.bounded("duration_s", duration_s, 10, 300)
                result = self.linkctl.force(FORCE_SPEEDS[action], seconds)
            else:
                raise ValueError(f"action must be renegotiate, force_10 or force_100, got {action!r}")
            self.sleep(LINK_SETTLE_S)
            return {**result, "link": asdict(self.link(self.iface))}
        return self._logged("link_control", args, body)

    def neighbour_scan(self) -> dict:
        def body():
            with self.linkctl.exclusive():
                self._require_link()
                lease = self.dhcp(self.iface)
                if not lease.ip:
                    raise ProbeError(f"{self.iface} has no IPv4 address to scan from")
                if lease.prefix is not None and lease.prefix < MAX_SCAN_PREFIX:
                    raise ProbeError(f"subnet /{lease.prefix} is too large to scan; limit is /{MAX_SCAN_PREFIX}")
                return probes.neighbour_scan(self.iface, run=self.run)
        return self._logged("neighbour_scan", {}, body)

    def speed_test(self, server: str, seconds: int = 10, reverse: bool = False) -> dict:
        args = {"server": server, "seconds": seconds, "reverse": reverse}

        def body():
            target = validate.host(server)
            duration = validate.bounded("seconds", seconds, 3, 30)
            backwards = validate.boolean("reverse", reverse)
            self._require_link()
            return probes.speed_test(self.iface, target, duration, backwards, run=self.run)
        return self._logged("speed_test", args, body)
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/pytest tests/test_mcp_service.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add ethtester/mcp/probelog.py ethtester/mcp/service.py tests/test_mcp_service.py
git commit -m "feat(mcp): ProbeService with validation, link checks and a probe log"
```

---

### Task 7: MCP server, auth and entry point

**Files:**
- Create: `ethtester/mcp/auth.py`, `ethtester/mcp/server.py`, `ethtester/mcp/__main__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `ProbeService` and `ProbeError` (Task 6), `LinkControl` (Task 5), `ProbeLog` (Task 6), and `load_config`/`DEFAULT_CONFIG_PATH` (existing).
- Produces:
  - `BearerAuth(app, token)` (ASGI) and `read_token(path: Path) -> str`;
  - `build_app(service, token) -> ASGI app` serving `/mcp`;
  - `resolve_bind(cfg, run=run_command) -> str`;
  - `main(argv=None)`;
  - `DEFAULT_TOKEN_PATH = Path("/etc/ethtester/mcp-token")`.

- [ ] **Step 1: Install the MCP dependencies in the dev venv and declare them**

```bash
.venv/bin/pip install "mcp>=1.13,<2" "uvicorn>=0.30"
.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; from mcp.server.transport_security import TransportSecuritySettings; print('ok')"
```
Expected: `ok`. If the second import fails, stop and report BLOCKED with the installed `mcp` version (`.venv/bin/pip show mcp`).

In `pyproject.toml`, under `[project.optional-dependencies]`, add:
```toml
mcp = ["mcp>=1.13,<2", "uvicorn>=0.30"]
```

- [ ] **Step 2: Write the failing test**

`tests/test_mcp_server.py`:
```python
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ethtester.config import Config
from ethtester.mcp.auth import read_token
from ethtester.mcp.server import build_app, resolve_bind
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
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ethtester.mcp.auth'`

- [ ] **Step 4: Implement**

`ethtester/mcp/auth.py`:
```python
"""Bearer-token gate in front of the MCP app."""

import hmac
from pathlib import Path


def read_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read MCP token {path}: {exc}") from None
    if not token:
        raise SystemExit(f"MCP token file {path} is empty")
    return token


class BearerAuth:
    """Rejects any HTTP request whose Authorization header isn't exactly 'Bearer <token>'."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return
        await self.app(scope, receive, send)
```

`ethtester/mcp/server.py`:
```python
"""MCP server: FastMCP over streamable HTTP, token-gated, listening on the Tailscale address only."""

import argparse
import functools
import logging
from pathlib import Path

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..checks.command import run_command
from ..config import DEFAULT_CONFIG_PATH, Config, load_config
from . import validate
from .auth import BearerAuth, read_token
from .linkctl import LinkControl
from .probelog import ProbeLog
from .service import ProbeService

log = logging.getLogger("ethtester.mcp")

DEFAULT_TOKEN_PATH = Path("/etc/ethtester/mcp-token")
INSTRUCTIONS = """Ethernet tester on a Raspberry Pi. Every probe runs through its Ethernet dongle only,
never Wi-Fi, so results describe the cable/port it is plugged into. Start with get_status. The dongle is
100 Mbit only, so 100M links are normal. dhcp_renew, link_control and neighbour_scan disturb the link
briefly and can't run at the same time; forced speeds revert to autonegotiation automatically."""


def build_app(service, token: str):
    mcp = FastMCP("ethtester", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def call(fn, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    @mcp.tool()
    async def get_status() -> dict:
        """Current verdict, every check row, and how old the data is (stale if > 30 s)."""
        return await call(service.get_status)

    @mcp.tool()
    async def get_history(limit: int = 50, since: str | None = None) -> dict:
        """Recent state changes (verdict, link, IP, switch port); limit 1-200, since = ISO time."""
        return await call(service.get_history, limit=limit, since=since)

    @mcp.tool()
    async def get_link_details() -> dict:
        """Speed, duplex, autonegotiation, advertised and link-partner modes, and NIC error counters."""
        return await call(service.get_link_details)

    @mcp.tool()
    async def ping(host: str, count: int = 5, size: int = 56) -> dict:
        """Ping a host through the Ethernet port; count 1-20, size 16-1472 bytes."""
        return await call(service.ping, host=host, count=count, size=size)

    @mcp.tool()
    async def traceroute(host: str, max_hops: int = 20) -> dict:
        """Trace the route to a host through the Ethernet port; max_hops 1-30."""
        return await call(service.traceroute, host=host, max_hops=max_hops)

    @mcp.tool()
    async def dns_lookup(name: str, record_type: str = "A", server: str | None = None) -> dict:
        """Resolve a name (A, AAAA, CNAME, MX, NS, TXT) via the DHCP DNS server or an IPv4 server."""
        return await call(service.dns_lookup, name=name, record_type=record_type, server=server)

    @mcp.tool()
    async def http_check(url: str) -> dict:
        """Fetch an http(s) URL: status, DNS/connect/TLS/first-byte timings, certificate expiry."""
        return await call(service.http_check, url=url)

    @mcp.tool()
    async def dhcp_renew() -> dict:
        """Drop and re-request the DHCP lease; reports seconds to IP and the new lease."""
        return await call(service.dhcp_renew)

    @mcp.tool()
    async def link_control(action: str, duration_s: int = 60) -> dict:
        """renegotiate, force_10 or force_100 (for 10-300 s, then autoneg is restored automatically)."""
        return await call(service.link_control, action=action, duration_s=duration_s)

    @mcp.tool()
    async def neighbour_scan() -> dict:
        """ARP-scan the local subnet (/22 or smaller): IP, MAC and vendor of each device found."""
        return await call(service.neighbour_scan)

    @mcp.tool()
    async def speed_test(server: str, seconds: int = 10, reverse: bool = False) -> dict:
        """iperf3 throughput to a server running 'iperf3 -s'; seconds 3-30; reverse = download."""
        return await call(service.speed_test, server=server, seconds=seconds, reverse=reverse)

    return BearerAuth(mcp.streamable_http_app(), token)


def resolve_bind(cfg: Config, run=run_command) -> str:
    if cfg.mcp_bind != "tailscale":
        return validate.ipv4(cfg.mcp_bind)
    addresses = run(["tailscale", "ip", "-4"], check=False).split()
    if not addresses:
        raise SystemExit("Tailscale has no IPv4 address yet; run 'sudo tailscale up' on the Pi")
    return validate.ipv4(addresses[0])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ethtester.mcp")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    token = read_token(args.token_file)
    bind = resolve_bind(cfg)
    linkctl = LinkControl(cfg.interface)
    linkctl.recover()
    probelog = ProbeLog(Path(cfg.log_path).with_name("probes.log"))
    app = build_app(ProbeService(cfg, linkctl, probelog=probelog), token)
    log.info("MCP server listening on http://%s:%d/mcp", bind, cfg.mcp_port)
    try:
        uvicorn.run(app, host=bind, port=cfg.mcp_port, log_level="info")
    finally:
        linkctl.restore_autoneg()
```

`ethtester/mcp/__main__.py`:
```python
from .server import main

main()
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v`
Expected: 6 passed. If `test_lists_all_tools` or `test_calls_tool` returns 421 or 400, the MCP SDK version is rejecting the Host header or needs initialisation. Report BLOCKED with the response body. Don't change the security settings on your own.

Then run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add ethtester/mcp/auth.py ethtester/mcp/server.py ethtester/mcp/__main__.py pyproject.toml tests/test_mcp_server.py
git commit -m "feat(mcp): token-gated streamable HTTP MCP server with 11 probe tools"
```

---

### Task 8: Deployment

**Files:**
- Create: `deploy/ethtester-mcp.service`
- Modify: `deploy/install.sh`, `deploy/ethtester.toml`, `README.md`

**Interfaces:**
- Consumes: `python -m ethtester.mcp --config /boot/firmware/ethtester.toml` (Task 7), and the token path `/etc/ethtester/mcp-token`.

- [ ] **Step 1: Write `deploy/ethtester-mcp.service`**

```ini
[Unit]
Description=ethtester MCP server (remote probes over Tailscale)
After=tailscaled.service ethtester.service
Wants=tailscaled.service

[Service]
WorkingDirectory=/opt/ethtester
ExecStart=/opt/ethtester/venv/bin/python -m ethtester.mcp --config /boot/firmware/ethtester.toml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Modify `deploy/install.sh`**

1. Directly after `set -euo pipefail`, add:
```bash
export DEBIAN_FRONTEND=noninteractive
```
2. Add `MCP_PORT` next to where `IFACE` is read from the config:
```bash
MCP_PORT=""
if [ -f "$CONFIG" ]; then
  MCP_PORT="$(sed -n 's/^mcp_port *= *\([0-9]*\).*/\1/p' "$CONFIG")"
  MCP_PORT="${MCP_PORT%%$'\n'*}"
fi
MCP_PORT="${MCP_PORT:-8765}"
```
3. Replace the `apt-get install -y python3-pil …` line with:
```bash
apt-get install -y python3-pil python3-spidev python3-gpiozero python3-lgpio lldpd curl fonts-dejavu-core \
  traceroute ethtool iperf3 arp-scan python3-venv
```
4. Directly after that apt line, add the Tailscale install:
```bash
if ! command -v tailscale >/dev/null; then
  CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/${CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/${CODENAME}.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled
```
5. Directly after the `find /opt/ethtester -name __pycache__ …` line, add the venv and token:
```bash
if [ ! -x /opt/ethtester/venv/bin/python ]; then
  python3 -m venv --system-site-packages /opt/ethtester/venv
fi
/opt/ethtester/venv/bin/pip install --quiet "mcp>=1.13,<2" "uvicorn>=0.30"

install -d -m 700 /etc/ethtester
if [ ! -s /etc/ethtester/mcp-token ]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(32))' > /etc/ethtester/mcp-token)
fi
```
6. Replace the block from `cp "$SRC/deploy/ethtester.service" …` to the end of the file with:
```bash
cp "$SRC/deploy/ethtester.service" /etc/systemd/system/ethtester.service
cp "$SRC/deploy/ethtester-mcp.service" /etc/systemd/system/ethtester-mcp.service
systemctl daemon-reload
systemctl enable ethtester ethtester-mcp
systemctl try-restart ethtester
systemctl restart ethtester-mcp

echo "Installed. On a first install, reboot to enable SPI; on a re-run the service has been restarted."
TS_NAME="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
if [ -z "$TS_NAME" ]; then
  echo "Next: run 'sudo tailscale up' on the Pi, open the link it prints, then re-run this installer."
else
  echo "Add the tester to Claude Code on your Mac with:"
  echo "  claude mcp add --transport http ethtester http://$TS_NAME:$MCP_PORT/mcp --header \"Authorization: Bearer $(cat /etc/ethtester/mcp-token)\""
fi
```

Run: `bash -n deploy/install.sh`
Expected: no output.

- [ ] **Step 3: Document the new config keys**

Append to `deploy/ethtester.toml`:
```toml
mcp_port       = 8765         # MCP server port (Tailscale only)
mcp_bind       = "tailscale"  # "tailscale" or an explicit IPv4 address
```

- [ ] **Step 4: Add a README section**

Append to `README.md`:
````markdown
## Remote probes (MCP)

The tester runs an MCP server, so Claude Code can read its status and run probes through the Ethernet port from anywhere. It's reachable only over Tailscale and needs a token.

1. Re-run the installer (`sudo ethernet-tester/deploy/install.sh`). It installs Tailscale and creates the token.
2. On the Pi, run `sudo tailscale up` and open the link it prints. Then re-run the installer once more: it prints the `claude mcp add …` command to use.
3. On your Mac, install Tailscale, sign in to the same account, and run that `claude mcp add` command.

Tools: `get_status`, `get_history`, `get_link_details`, `ping`, `traceroute`, `dns_lookup`, `http_check`, `dhcp_renew`, `link_control`, `neighbour_scan`, `speed_test`.

Every call is logged to `/var/log/ethtester/probes.log`. To rotate the token, delete `/etc/ethtester/mcp-token` and re-run the installer.
````

- [ ] **Step 5: Commit**

```bash
git add deploy/ethtester-mcp.service deploy/install.sh deploy/ethtester.toml README.md
git commit -m "feat(deploy): install Tailscale, the MCP venv, token and ethtester-mcp service"
```

---

### Task 9: On-device verification

This is a manual test on the Pi. Commit only if a fix is needed, and put any fix through the relevant task's test cycle first.

- [ ] **Step 1:** Copy the code to the Pi and have the user re-run `sudo ~/ethernet-tester/deploy/install.sh`, then `sudo tailscale up`, then the installer once more. Check that `systemctl is-active ethtester-mcp` prints `active`.
- [ ] **Step 2:** On the Mac, with Tailscale signed in, run the printed `claude mcp add` command. Check that `claude mcp list` shows `ethtester` as connected.
- [ ] **Step 3:** Run `curl -s -o /dev/null -w '%{http_code}' http://<ts-name>:8765/mcp`. Expected: `401`. Then, from a LAN machine, run `curl --max-time 3 http://192.168.1.222:8765/mcp`. Expected: connection refused or timeout.
- [ ] **Step 4:** Call each tool once from Claude Code. `link_control force_10` with `duration_s` 20 should show the 10 Mbit link and then revert by itself, which you can confirm with `get_link_details` after 25 s. Check that `/var/log/ethtester/probes.log` has one line per call.

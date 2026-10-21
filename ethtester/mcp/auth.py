"""Bearer-token gate in front of the MCP app."""

import hmac
import ipaddress
from pathlib import Path

ALLOWED_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                         ("100.64.0.0/10", "127.0.0.0/8", "::1/128", "fd7a:115c:a1e0::/48"))
LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fe80::/10"))
CLIENT_MODES = ("tailscale", "lan")
TEST_CLIENT_HOST = "testclient"  # Starlette's TestClient; not an address a real socket can have


def client_allowed(client, clients: str = "tailscale") -> bool:
    """True for Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48) and loopback peers, or no peer info.
    With clients="lan", private home-network ranges (RFC 1918, fe80::/10) are allowed too."""
    if not client:
        return True
    host = client[0]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host == TEST_CLIENT_HOST
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    networks = ALLOWED_NETWORKS + LAN_NETWORKS if clients == "lan" else ALLOWED_NETWORKS
    return any(addr in net for net in networks)


def read_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read MCP token {path}: {exc}") from None
    if not token:
        raise SystemExit(f"MCP token file {path} is empty")
    return token


class BearerAuth:
    """Passes lifespan through, drops other non-HTTP scopes, rejects HTTP requests from outside
    the allowed networks (403; Tailscale/loopback, plus private LAN ranges in "lan" mode), and any whose Authorization header isn't exactly 'Bearer <token>' (401)."""

    def __init__(self, app, token: str, clients: str = "tailscale"):
        if clients not in CLIENT_MODES:
            raise ValueError(f"clients must be one of {CLIENT_MODES}, got {clients!r}")
        self.app = app
        self.clients = clients
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if not client_allowed(scope.get("client"), self.clients):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "forbidden"}'})
                return
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return
        elif scope["type"] != "lifespan":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)

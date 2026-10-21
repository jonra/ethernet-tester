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

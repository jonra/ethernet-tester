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

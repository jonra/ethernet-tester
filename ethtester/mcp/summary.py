"""Short, screen-sized summaries of probe results for the tester's probe screen."""

import logging
import textwrap

log = logging.getLogger(__name__)

MAX_LINES = 4
ERROR_LINES = 2
LINE_CHARS = 37  # what fits across the 250 px screen in the 11 px row font


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _ping(r: dict) -> list[str]:
    lines = [f"sent {r['sent']}  received {r['received']}  loss {r['loss_pct']:.0f}%"]
    rtt = r.get("rtt_ms")
    if rtt:
        lines.append(f"rtt {rtt['min']:.1f} / {rtt['avg']:.1f} / {rtt['max']:.1f} ms")
    return lines


def _traceroute(r: dict) -> list[str]:
    hops = r["hops"]
    lines = [_plural(len(hops), "hop")]
    for hop in hops[-3:]:
        rtt = f"{hop['rtt_ms']:.1f} ms" if hop.get("rtt_ms") is not None else "*"
        lines.append(f"#{hop['hop']} {hop.get('ip') or '*'} {rtt}")
    return lines


def _dns_lookup(r: dict) -> list[str]:
    lines = [f"{r['rcode']} via {r['server']}  {r['ms']:.0f}ms"]
    return lines + [str(a["data"]) for a in r["answers"][:3]]


def _http_check(r: dict) -> list[str]:
    if not r.get("ok") and r.get("error"):
        lines = [str(r["error"])]
    else:
        total = (r.get("ms") or {}).get("total")
        lines = [f"HTTP {r.get('status')}" + (f"  total {total:.0f}ms" if total is not None else "")]
    if r.get("cert_expires"):
        lines.append(f"cert expires {r['cert_expires']}")
    return lines


def _dhcp_renew(r: dict) -> list[str]:
    if not r.get("ok"):
        return [str(r.get("error") or "failed")]
    address = f"{r['ip']}/{r['prefix']}" if r.get("prefix") is not None else str(r["ip"])
    return [f"{address} in {r['seconds_to_ip']:g}s",
            f"gw {r.get('gateway') or '-'}  dhcp {r.get('dhcp_server') or '-'}"]


def _link_control(r: dict) -> list[str]:
    lines = [str(r["action"])]
    if r.get("reverts_in_s") is not None:
        lines.append(f"reverts in {r['reverts_in_s']:g}s")
    link = r.get("link") or {}
    if link.get("up"):
        speed = f"{link['speed_mbps']}M" if link.get("speed_mbps") else "?M"
        lines.append(f"link up {speed} {link.get('duplex') or '?'}")
    else:
        lines.append("link down")
    return lines


def _neighbour_scan(r: dict) -> list[str]:
    found = r["neighbours"]
    lines = [_plural(len(found), "device")]
    return lines + [f"{n['ip']} {n.get('vendor') or n.get('mac') or ''}".rstrip() for n in found[:3]]


def _speed_test(r: dict) -> list[str]:
    if not r.get("ok"):
        return [str(r.get("error") or "failed")]
    lines = [f"{r['mbps']:g} Mbit/s ({'download' if r.get('reverse') else 'upload'})"]
    if r.get("retransmits") is not None:
        lines.append(f"retransmits {r['retransmits']}")
    return lines


SUMMARISERS = {"ping": _ping, "traceroute": _traceroute, "dns_lookup": _dns_lookup,
               "http_check": _http_check, "dhcp_renew": _dhcp_renew, "link_control": _link_control,
               "neighbour_scan": _neighbour_scan, "speed_test": _speed_test}


def summarise(tool: str, args: dict, result) -> list[str]:
    """At most 4 lines describing a finished probe's result; [] if the result has an odd shape."""
    fn = SUMMARISERS.get(tool)
    if fn is None or not isinstance(result, dict):
        return []
    try:
        return [str(line) for line in fn(result)][:MAX_LINES]
    except Exception:
        log.debug("could not summarise %s result", tool, exc_info=True)
        return []


def outcome(tool: str, result) -> str:
    """"error" when the probe ran but reported failure (ok false, or a ping with no replies)."""
    if not isinstance(result, dict):
        return "ok"
    if result.get("ok") is False:
        return "error"
    if tool == "ping" and result.get("received") == 0:
        return "error"
    return "ok"


def error_summary(message: str) -> list[str]:
    """The first line of an error message, wrapped onto at most 2 screen lines."""
    first = (str(message).strip().splitlines() or [""])[0].strip() or "error"
    return textwrap.wrap(first, LINE_CHARS, max_lines=ERROR_LINES, placeholder="…") or ["error"]


def target_of(tool: str, args: dict) -> str:
    """What the probe is aimed at, for the probe screen's header."""
    if tool == "dns_lookup":
        name, qtype = args.get("name"), str(args.get("record_type") or "A").upper()
        return f"{name}" + ("" if qtype == "A" else f" {qtype}")
    key = {"ping": "host", "traceroute": "host", "http_check": "url", "speed_test": "server",
           "link_control": "action"}.get(tool)
    value = args.get(key) if key else None
    return "" if value is None else str(value)

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
        if length < 3:
            raise ValueError("short MX record")
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
        if offset + length > len(data):
            raise ValueError("truncated DNS record data")
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

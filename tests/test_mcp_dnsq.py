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


def test_rejects_truncated_rdata():
    with pytest.raises(ValueError):
        parse_answer(response(6, 0x8180, 15, [(15, 60, b"\x00")]), 6)
    data = response(5, 0x8180, 1, [(1, 300, bytes([1, 2, 3, 4]))])
    with pytest.raises(ValueError):
        parse_answer(data[:-2], 5)

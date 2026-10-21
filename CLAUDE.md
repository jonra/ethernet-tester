# ethernet-tester

## Testing the network with the ethtester CLI

`ethtester` (see "Command-line client (Mac)" in README.md) runs probes from the Raspberry Pi tester through its Ethernet port only. Run `ethtester --help` for all commands. Output is JSON.

- Start with `ethtester status` (verdict and checks; stale if older than 30 s). Use `link` for speed/duplex/errors and `history` for past changes.
- Reachability and path: `ping HOST`, `traceroute HOST`. Name resolution: `dns NAME [-t TYPE] [--server IP]`. Web: `http URL`.
- Who is on the network: `neighbours`. Throughput: `speedtest SERVER` (needs `iperf3 -s` on SERVER).
- `renew-dhcp` and `link-control` briefly disturb the tested port. The tester's screen and `history` will show NO DHCP, CABLE? or extra flaps; those are caused by the test. Don't run them back to back.
- Exit codes: 0 OK, 1 tool error (the probe failed, e.g. no link), 2 connection or config error (check the URL, and the token if the tester uses one, with `ethtester configure`; a tester with `mcp_auth = "none"` needs no token).
- The dongle is 100 Mbit only, so a 100M link is normal, not a fault.

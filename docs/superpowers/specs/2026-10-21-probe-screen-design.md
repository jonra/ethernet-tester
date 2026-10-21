# Two Screens: Ethernet and Probe — Design

Date: 2026-10-21

## Goal

The e-ink display has two screens:
- the **Ethernet screen** (today's status screen), which is the default;
- a **probe screen**, which appears when Claude runs a probe through the MCP server or the `ethtester` CLI and shows what is running and its result.

## Behaviour

- **Which tools show the probe screen:** every tool except the read-only ones (`get_status`, `get_history`, `get_link_details`).
- **While a probe is running:** the probe screen shows the tool, its target and `RUNNING…`.
- **When it finishes:** the screen shows a short result summary and `OK` or `FAILED`.
- **When it goes back to the Ethernet screen:** the probe screen stays up until either of these happens first:
  - the Ethernet verdict code changes compared with the **baseline**, or
  - **5 minutes** have passed since the probe finished.
- **Baseline:** the verdict code at the first tick after the probe's `settle_until` time. This stops a probe's own disturbance from immediately dismissing its result:
  - `dhcp_renew`: `settle_until` = finished + 10 s.
  - `link_control` force: `settle_until` = finished + duration_s + 10 s, so the forced period and the revert are ignored.
  - `link_control renegotiate`: finished + 10 s.
  - All other tools: `settle_until` = finished.
- **A new probe** replaces the previous one on the screen and restarts these rules.
- **Polling:** the display loop polls every 0.5 s (fast) while a probe is running, so RUNNING and the result appear quickly.

## Data flow

The MCP service and the display loop are separate processes. They share one file, `/run/ethtester/activity.json`, written atomically:

```json
{"id": "1729500000.123", "tool": "ping", "target": "1.1.1.1", "state": "running|ok|error",
 "started": 1729500000.1, "finished": null, "settle_until": null,
 "summary": ["sent 5  received 5  loss 0%", "rtt 1.0 / 1.5 / 2.0 ms"], "calls": 3}
```

- `id` changes for every probe.
- `calls` counts the screen-worthy calls since the MCP server started.
- On an error, `summary` holds the first line of the error message, at most 2 lines.
- If writing the activity file fails, the failure is logged and the tool result is unaffected.
- If reading the file fails or the file is missing, the loop shows the Ethernet screen.

## Probe screen layout (250×122)

```
PROBE  ping 1.1.1.1
────────────────────────────
sent 5  received 5  loss 0%
rtt 1.0 / 1.5 / 2.0 ms
(up to 4 summary lines)
────────────────────────────
OK                3 calls 10:42
```
- The header uses the bold font. Summary lines use the row font and are truncated with `…`.
- The footer verdict is `RUNNING…`, `OK` or `FAILED` in the bold font.
- It is drawn with the same fonts and helpers as `render.py`.
- Screen changes cause a full refresh; updates within the probe screen are partial (display code `PROBE:<state>`).

## Summaries per tool (at most 4 lines)

| Tool | Summary lines |
|---|---|
| ping | `sent N  received N  loss X%`, `rtt min / avg / max ms` (if any) |
| traceroute | `N hops`, then the last 3 hops as `#n ip rtt` |
| dns_lookup | `RCODE via server  Nms`, then up to 3 answer `data` values |
| http_check | `HTTP status  total Nms` (or the error), `cert expires …` if present |
| dhcp_renew | `ip/prefix in Ns` (or the error), `gw X  dhcp Y` |
| link_control | `action`, `reverts in Ns` if forced, `link up 100M full` or `link down` |
| neighbour_scan | `N devices`, then the first 3 as `ip vendor` |
| speed_test | `N Mbit/s (download/upload)`, `retransmits N` (or the error) |

## Components

- `ethtester/activity.py` (stdlib): `write_activity(path, data)` (atomic, the same approach as statefile), and `read_activity(path) -> dict | None`, which never raises.
- `ethtester/screens.py`: `ScreenSelector(hold_s=300)`.
  - `choose(activity, verdict_code, now) -> "probe" | "ethernet"` keeps per-`id` state: the baseline and a dismissed flag.
  - `fast(activity) -> bool` is True while the activity is running.
- `ethtester/render.py`: `render_probe(activity, clock_text) -> Image`.
- `ethtester/main.py`: `run()` reads the activity each tick, asks the selector which screen to show, renders and displays it, and uses the fast interval while `fast()` is true.
- `ethtester/mcp/summary.py`: `summarise(tool, args, result) -> list[str]` and `target_of(tool, args) -> str`.
- `ethtester/mcp/service.py`: `_logged` writes the running and finished activity for screen-worthy tools, and computes `settle_until`.
- `Config.activity_path = "/run/ethtester/activity.json"`.

## Testing

- Selector rules:
  - running → probe;
  - finished within 5 min with an unchanged verdict → probe;
  - verdict change after settle → ethernet, and it stays ethernet for that id;
  - verdict change before settle → still probe;
  - 5 min passed → ethernet;
  - a new id → probe again;
  - no activity → ethernet.
- The summary for every tool, including errors.
- Activity read/write: atomic, tolerant of corrupt files.
- The service writes running and finished activity, but not for read-only tools.
- `render_probe` produces 250×122 images for running, OK and error states, saved to `tests/output/` for review.
- `run()` switches screens and uses the fast interval while running.

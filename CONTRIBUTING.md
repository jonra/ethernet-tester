# Contributing

Issues and pull requests are welcome.

- Keep the code compatible with Python 3.11 and Pillow 9.4. Older Raspberry Pi OS releases ship those versions.
- Every network check or probe must be bound to the tested interface, and external commands are argv lists, never a shell.
- Write a failing test first, then the fix (`.venv/bin/pytest`). The tests must run without the Pi hardware.
- Display changes: check the PNGs in `tests/output/` for overlap or clipping.
- Installer changes: keep `deploy/install.sh` safe to re-run and check it with `bash -n`.

Please describe how you tested on real hardware (Pi model, dongle, HAT version) when a change touches it.

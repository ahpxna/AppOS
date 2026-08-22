#!/usr/bin/env python3
"""Record local pointer positions after explicit local user start.

This utility never moves, clicks, or injects pointer events.  On macOS, grant
Accessibility permission only if you choose to run it; trace files are private
behavioural data and should stay outside version control.
"""
from __future__ import annotations

import argparse
import csv
import signal
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Consent-based local pointer telemetry recorder")
    parser.add_argument("--output", required=True, help="CSV output path")
    args = parser.parse_args()
    try:
        from pynput import mouse
    except ImportError as exc:
        raise SystemExit("Install pynput in the environment that records: pip install pynput") from exc

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t_seconds", "x", "y"])

        def on_move(x, y):
            writer.writerow([f"{time.monotonic():.9f}", x, y])
            handle.flush()

        print("Recording local pointer movements. Press Ctrl-C to stop.")
        listener = mouse.Listener(on_move=on_move)
        listener.start()
        while running:
            time.sleep(0.2)
        listener.stop()
        listener.join()
    print(f"Saved local trace: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

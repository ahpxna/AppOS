#!/usr/bin/env python3
"""Launch Chrome with an isolated, persistent JobOS browser profile.

The operator signs into LinkedIn in this window one time. JobOS then attaches
to its loopback-only Chrome DevTools endpoint; it never reads/imports cookies
from another browser profile and never receives a password.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT / "data" / "browser-profiles" / "jobos-linkedin"


def find_chrome() -> str:
    if sys.platform == "darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if Path(path).is_file():
            return path
    if os.name == "nt":
        roots = [os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", ""),
                 os.getenv("LOCALAPPDATA", "")]
        for root in roots:
            path = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if path.is_file():
                return str(path)
    for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(binary)
        if found:
            return found
    raise RuntimeError("Google Chrome/Chromium was not found. Install Chrome or pass --chrome-bin.")


def cdp_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the isolated JobOS LinkedIn Chrome profile.")
    parser.add_argument("--chrome-bin")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--wait-seconds", type=int, default=10)
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("--port must be a valid non-privileged TCP port.")
    profile_dir = args.profile_dir.expanduser().resolve()
    chrome = args.chrome_bin or find_chrome()
    chrome_args = [
        f"--remote-debugging-port={args.port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run", "--no-default-browser-check", "https://www.linkedin.com/jobs/",
    ]
    # On macOS, launching the app executable directly can hand arguments to
    # the already-running normal Chrome instance, which then ignores CDP.
    # open -n forces a separate app instance for the isolated profile.
    command = (
        ["open", "-na", "Google Chrome", "--args", *chrome_args]
        if sys.platform == "darwin"
        else [chrome, *chrome_args]
    )
    if args.print_command:
        print(" ".join(command))
        return 0
    if cdp_ready(args.port):
        print(f"JobOS browser CDP is already reachable at http://127.0.0.1:{args.port}.")
        return 0
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(command, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        if cdp_ready(args.port):
            print(f"Started isolated JobOS browser profile: {profile_dir}")
            print("Sign into LinkedIn manually in that Chrome window, then leave it open.")
            return 0
        time.sleep(0.25)
    raise SystemExit(
        f"Chrome started but CDP did not open on 127.0.0.1:{args.port}. "
        "Close any Chrome using that port, then retry."
    )


if __name__ == "__main__":
    raise SystemExit(main())

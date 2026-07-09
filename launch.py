"""Start the viewer servers a deck needs, then open the deck in the browser.

For each embed in the manifest:
  - static : ensure the single shim-injecting static server (rooted at the
             deck's static_root) is running on STATIC_PORT.
  - proxy  : start the backend (its own server, if a cmd is given), wait for it
             to answer, then start a shim-injecting proxy in front of it.
             External sites (backend.base) are proxied without launching a cmd.

Everything runs locally, so the deck is fully interactive and offline (for
local viewers) during a talk. Ctrl-C tears all child processes down.

Usage:
    python launch.py --manifest manifest.json
    python launch.py --manifest manifest.json --build
    python launch.py --manifest manifest.json --no-open
"""
from __future__ import annotations

import argparse
import atexit
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from deckcommon import (
    STATIC_PORT,
    backend_base,
    get_static_root,
    load_manifest,
    manifest_dir,
    needs_static_server,
    resolve_embeds,
)

_PROCS: list[subprocess.Popen] = []


def _spawn(cmd: list[str], cwd: Path) -> subprocess.Popen:
    print(f"[launch] $ {' '.join(cmd)}  (cwd={cwd})")
    p = subprocess.Popen(cmd, cwd=str(cwd))
    _PROCS.append(p)
    return p


def _cleanup():
    for p in _PROCS:
        if p.poll() is None:
            p.terminate()
    for p in _PROCS:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _wait_for_port(port: int, timeout: float = 30.0, path: str = "/") -> bool:
    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # server answered (even a 404 means it's up)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.4)
    return False


def main():
    ap = argparse.ArgumentParser(description="Launch viewer servers and open the deck")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--build", action="store_true", help="Run build_deck.py first")
    ap.add_argument("--no-open", action="store_true", help="Do not open the browser")
    args = ap.parse_args()

    atexit.register(_cleanup)

    manifest = load_manifest(args.manifest)
    embeds = resolve_embeds(manifest)
    mdir = manifest_dir(manifest)
    here = Path(__file__).resolve().parent
    serve_py = here / "serve.py"

    if args.build:
        subprocess.check_call(
            [sys.executable, str(here / "build_deck.py"), "--manifest", str(args.manifest)]
        )

    # 1) One static server for all static viewers.
    if needs_static_server(embeds):
        _spawn(
            [sys.executable, str(serve_py), "--mode", "static",
             "--port", str(STATIC_PORT), "--root", str(get_static_root(manifest))],
            cwd=mdir,
        )

    # 2) Per proxy viewer: local backend (optional) + shim-injecting proxy.
    started_backends: set[str] = set()
    for e in embeds:
        if e.get("serve") != "proxy":
            continue
        backend = e.get("backend") or {}
        base = backend_base(e)
        bport = backend.get("port")
        if backend.get("cmd") and base not in started_backends:
            _spawn(backend["cmd"], cwd=(mdir / backend.get("cwd", ".")))
            started_backends.add(base)
            if bport and not _wait_for_port(bport, timeout=60):
                print(f"[launch] WARNING: backend on :{bport} did not come up in time")
        _spawn(
            [sys.executable, str(serve_py), "--mode", "proxy",
             "--port", str(e["serve_port"]), "--backend", base],
            cwd=mdir,
        )

    for port in {STATIC_PORT} | {e["serve_port"] for e in embeds}:
        _wait_for_port(port, timeout=15)

    deck = mdir / "deck.html"
    print(f"[launch] deck ready: {deck}")
    if not args.no_open:
        if not deck.exists():
            print("[launch] deck.html not found - run with --build first.")
        else:
            webbrowser.open(deck.as_uri())

    print("[launch] servers running. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[launch] shutting down.")


if __name__ == "__main__":
    main()

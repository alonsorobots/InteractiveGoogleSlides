"""Auto-sync a Google Slides deck to a local PDF (no Drive API / OAuth).

Google serves any *link-viewable* presentation as a PDF at:
    https://docs.google.com/presentation/d/<ID>/export/pdf
So for a deck shared as "Anyone with the link -> Viewer", we just pull that URL,
drop it next to the manifest, and (optionally) rebuild the deck. With --watch it
polls and rebuilds whenever the deck's bytes change.

Setup once: in Google Slides, Share -> General access -> "Anyone with the link"
(Viewer). Copy the URL.

Usage:
    # one-shot pull + build
    python sync_slides.py --url "<slides url>" --manifest manifest.json --build

    # keep syncing every 30s while you iterate
    python sync_slides.py --url "<slides url>" --manifest manifest.json --build --watch 30

Point your manifest's "pdf" field at the downloaded file (default deck.pdf).
The --build step needs PyMuPDF (pip install pymupdf).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_ID_RE = re.compile(r"/presentation/d/([a-zA-Z0-9_-]+)")


def extract_id(url_or_id: str) -> str:
    m = _ID_RE.search(url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url_or_id):
        return url_or_id
    raise SystemExit(f"Could not extract a presentation ID from: {url_or_id!r}")


def download_pdf(pres_id: str) -> bytes:
    url = f"https://docs.google.com/presentation/d/{pres_id}/export/pdf"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (slides-sync)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if data[:4] != b"%PDF":
        raise SystemExit(
            "Export did not return a PDF. Make sure the deck is shared as "
            "'Anyone with the link -> Viewer'."
        )
    return data


def rebuild(manifest: Path):
    build = Path(__file__).resolve().parent / "build_deck.py"
    subprocess.check_call([sys.executable, str(build), "--manifest", str(manifest)])


def pull_once(pres_id: str, out: Path, manifest: Path | None, build: bool) -> str:
    data = download_pdf(pres_id)
    digest = hashlib.sha256(data).hexdigest()[:12]
    changed = not out.exists() or hashlib.sha256(out.read_bytes()).hexdigest()[:12] != digest
    if changed:
        out.write_bytes(data)
        print(f"[sync] updated {out} ({len(data)} bytes, {digest})")
        if build and manifest:
            rebuild(manifest)
    else:
        print(f"[sync] unchanged ({digest})")
    return digest


def main():
    ap = argparse.ArgumentParser(description="Sync a Google Slides deck to a local PDF")
    ap.add_argument("--url", required=True, help="Google Slides URL or presentation ID")
    ap.add_argument("--out", type=Path, default=None, help="Output PDF (default: next to manifest, deck.pdf)")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--build", action="store_true", help="Rebuild deck.html after a change")
    ap.add_argument("--watch", type=float, default=0, help="Poll interval in seconds (0 = one-shot)")
    args = ap.parse_args()

    pres_id = extract_id(args.url)
    if args.out:
        out = args.out.resolve()
    elif args.manifest:
        out = args.manifest.resolve().parent / "deck.pdf"
    else:
        out = Path("deck.pdf").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.build and not args.manifest:
        ap.error("--build requires --manifest")

    print(f"[sync] presentation {pres_id} -> {out}")
    pull_once(pres_id, out, args.manifest, args.build)

    if args.watch > 0:
        print(f"[sync] watching every {args.watch}s (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(args.watch)
                try:
                    pull_once(pres_id, out, args.manifest, args.build)
                except Exception as e:
                    print(f"[sync] error: {e}")
        except KeyboardInterrupt:
            print("\n[sync] stopped.")


if __name__ == "__main__":
    main()

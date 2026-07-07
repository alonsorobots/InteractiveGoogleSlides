"""Locate overlay misalignment by differencing renders.

For a given slide, render it once as-is and once with its media elements deleted
(on a throwaway copy). The pixels that changed are EXACTLY where Google draws the
element. Compare that measured box to the rect slides_import computes.

  python diag_offset.py --slide 12
  python diag_offset.py --all
"""
from __future__ import annotations

import argparse
import io
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

import slides_import as si
import slides_auth_picker as pk


def _thumb(slides, pid, page_id, size="LARGE") -> np.ndarray:
    t = (slides.presentations().pages().getThumbnail(
        presentationId=pid, pageObjectId=page_id,
        thumbnailProperties_thumbnailSize=size,
        thumbnailProperties_mimeType="PNG").execute())
    req = urllib.request.Request(t["contentUrl"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return np.asarray(Image.open(io.BytesIO(r.read())).convert("RGB"))


def _bbox_of_diff(a: np.ndarray, b: np.ndarray, thresh: int = 24):
    h = min(a.shape[0], b.shape[0]); w = min(a.shape[1], b.shape[1])
    a, b = a[:h, :w], b[:h, :w]
    diff = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    mask = diff > thresh
    if not mask.any():
        return None, (w, h)
    ys, xs = np.nonzero(mask)
    return (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1), (w, h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", type=Path, default=Path("gesture_deck/token_picker.json"))
    ap.add_argument("--slide", type=int, default=12)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--size", default="LARGE")
    ap.add_argument("--draw", action="store_true",
                    help="save _diag/slide-NNN.png with our computed rects outlined")
    args = ap.parse_args()

    creds = pk._load_cached(args.token)
    if not creds:
        raise SystemExit(f"No cached creds at {args.token}")
    from googleapiclient.discovery import build
    slides = build("slides", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    fid = __import__("json").loads(Path("gesture_deck/picker_state.json").read_text())["file_id"]

    pres = slides.presentations().get(presentationId=fid).execute()
    page = pres["pageSize"]
    pw = si._emu(float(page["width"]["magnitude"]), page["width"].get("unit"))
    ph = si._emu(float(page["height"]["magnitude"]), page["height"].get("unit"))
    deck = pres.get("slides", [])
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    targets = range(1, len(deck) + 1) if args.all else [args.slide]

    print("[diag] making a throwaway copy to render 'without media'…")
    copy = drive.files().copy(fileId=fid, body={"name": "[diag temp]"}, fields="id",
                              supportsAllDrives=True).execute()
    cid = copy["id"]
    try:
        copy_pres = slides.presentations().get(presentationId=cid).execute()
        copy_deck = copy_pres.get("slides", [])
        for idx in targets:
            oslide = deck[idx - 1]
            cslide = copy_deck[idx - 1]
            media = list(si._iter_media(oslide.get("pageElements", []), identity))
            if not media:
                print(f"\nslide {idx}: no media, skipping")
                continue

            # our computed union rect (percent) over all media on this slide
            rects = []
            for kind, el, xf in media:
                size = el.get("size")
                if size:
                    rects.append(si._rect_percent(xf, size, pw, ph))
            ox0 = min(r["x"] for r in rects); oy0 = min(r["y"] for r in rects)
            ox1 = max(r["x"] + r["w"] for r in rects); oy1 = max(r["y"] + r["h"] for r in rects)

            # delete this slide's media on the copy, render clean
            cmedia = list(si._iter_media(cslide.get("pageElements", []), identity))
            del_ids = [c[1].get("objectId") for c in cmedia if c[1].get("objectId")]
            # restore first (in case of --all reusing copy): re-copy per slide is simpler,
            # but deletes are cumulative on distinct ids, which is fine.
            slides.presentations().batchUpdate(
                presentationId=cid,
                body={"requests": [{"deleteObject": {"objectId": i}} for i in del_ids]},
            ).execute()

            orig = _thumb(slides, fid, oslide["objectId"], args.size)

            if args.draw:
                from PIL import ImageDraw
                im = Image.fromarray(orig.copy())
                dr = ImageDraw.Draw(im)
                Wd, Hd = im.size
                for r in rects:
                    x0 = r["x"] / 100 * Wd; y0 = r["y"] / 100 * Hd
                    x1 = (r["x"] + r["w"]) / 100 * Wd; y1 = (r["y"] + r["h"]) / 100 * Hd
                    dr.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=3)
                Path("_diag").mkdir(exist_ok=True)
                im.save(f"_diag/slide-{idx:03d}.png")
                print(f"slide {idx}: wrote _diag/slide-{idx:03d}.png (red = our computed box)")

            clean = _thumb(slides, cid, cslide["objectId"], args.size)
            box, (W, H) = _bbox_of_diff(orig, clean)
            if box is None:
                print(f"\nslide {idx}: no pixel difference detected (?)")
                continue
            mx0, my0, mx1, my1 = box
            # measured rect as percent of slide
            m = {"x": mx0 / W * 100, "y": my0 / H * 100,
                 "w": (mx1 - mx0) / W * 100, "h": (my1 - my0) / H * 100}
            o = {"x": ox0, "y": oy0, "w": ox1 - ox0, "h": oy1 - oy0}

            print(f"\nslide {idx}: {len(media)} media element(s), thumb {W}x{H}")
            print(f"  ours (computed) : x={o['x']:.2f}% y={o['y']:.2f}% w={o['w']:.2f}% h={o['h']:.2f}%")
            print(f"  measured (real) : x={m['x']:.2f}% y={m['y']:.2f}% w={m['w']:.2f}% h={m['h']:.2f}%")
            dxp = (m['x'] - o['x']) / 100 * W
            dyp = (m['y'] - o['y']) / 100 * H
            dwp = (m['w'] - o['w']) / 100 * W
            dhp = (m['h'] - o['h']) / 100 * H
            print(f"  delta           : dx={dxp:+.1f}px dy={dyp:+.1f}px dw={dwp:+.1f}px dh={dhp:+.1f}px")
    finally:
        try:
            drive.files().delete(fileId=cid, supportsAllDrives=True).execute()
            print(f"\n[diag] deleted temp copy {cid}")
        except Exception as e:
            print(f"\n[diag] could not delete temp copy {cid}: {e!r}")


if __name__ == "__main__":
    main()

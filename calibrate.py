"""Measure how Google Slides' rendered geometry maps to our computed overlay boxes.

The overlay placement in slides_import assumes an element rendered at
(translateX, translateY) with size (w*scaleX, h*scaleY) lands at the same
fraction of the slide in Google's `getThumbnail` render. If overlays look a few
pixels off, this probe finds out *why*:

  1. create a throwaway presentation,
  2. paint the slide black and drop white squares at precisely known EMU spots
     (a grid, so position and size vary independently),
  3. render it through the exact same getThumbnail path,
  4. detect each white square's real pixel box,
  5. fit expected-fraction -> measured-fraction for x, y, width, height and report
     slope (scale), intercept (translation), and residuals (linearity).

If slope~=1 and intercept~=0 with tiny residuals, our mapping is already exact.
Otherwise the fitted slope/intercept *is* the correction to apply.

Usage:
  python calibrate.py                       # uses gesture_deck/token_picker.json
  python calibrate.py --token path/to/token.json --keep
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

EMU_PER_INCH = 914400


def _svc(creds):
    from googleapiclient.discovery import build

    slides = build("slides", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return slides, drive


def _emu(v: dict) -> float:
    mag = float(v["magnitude"])
    return mag * (914400 / 72) if v.get("unit") == "PT" else mag


def _build_probe(slides, grid: int, sq_in: float):
    """Create a presentation with a black slide + a grid of white squares.
    Returns (presentation_id, slide_id, page_w_emu, page_h_emu, squares)
    where squares = list of dicts with expected EMU/fraction geometry.
    """
    pres = slides.presentations().create(body={"title": "[slides-import calibration probe]"}).execute()
    pid = pres["presentationId"]
    page = pres["pageSize"]
    pw = _emu(page["width"])
    ph = _emu(page["height"])
    slide_id = pres["slides"][0]["objectId"]

    # Grid of center fractions inset from the edges so squares stay fully on-slide.
    fracs = [(i + 1) / (grid + 1) for i in range(grid)]
    # Vary each square's size across the grid so the width/height fit is real
    # (a slope needs varying inputs; constant-size squares can't reveal a scale).
    cells = grid * grid
    sizes = [round((sq_in * (0.6 + 0.8 * k / max(1, cells - 1))) * EMU_PER_INCH) for k in range(cells)]

    requests = [
        {
            "updatePageProperties": {
                "objectId": slide_id,
                "fields": "pageBackgroundFill.solidFill.color",
                "pageProperties": {
                    "pageBackgroundFill": {
                        "solidFill": {"color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}}
                    }
                },
            }
        }
    ]
    squares = []
    n = 0
    for fy in fracs:
        for fx in fracs:
            oid = f"probe_{n}"
            sq = sizes[n]
            tx = fx * pw - sq / 2
            ty = fy * ph - sq / 2
            requests.append({
                "createShape": {
                    "objectId": oid,
                    "shapeType": "RECTANGLE",
                    "elementProperties": {
                        "pageObjectId": slide_id,
                        "size": {
                            "width": {"magnitude": sq, "unit": "EMU"},
                            "height": {"magnitude": sq, "unit": "EMU"},
                        },
                        "transform": {
                            "scaleX": 1, "scaleY": 1,
                            "translateX": tx, "translateY": ty, "unit": "EMU",
                        },
                    },
                }
            })
            requests.append({
                "updateShapeProperties": {
                    "objectId": oid,
                    "fields": "shapeBackgroundFill.solidFill.color,outline.propertyState",
                    "shapeProperties": {
                        "shapeBackgroundFill": {
                            "solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}
                        },
                        "outline": {"propertyState": "NOT_RENDERED"},
                    },
                }
            })
            squares.append({
                "id": oid,
                "tx": tx, "ty": ty, "size": sq,
                # expected fractions of the page for the square's box
                "fx0": tx / pw, "fy0": ty / ph,
                "fw": sq / pw, "fh": sq / ph,
                "fcx": (tx + sq / 2) / pw, "fcy": (ty + sq / 2) / ph,
            })
            n += 1

    slides.presentations().batchUpdate(presentationId=pid, body={"requests": requests}).execute()
    return pid, slide_id, pw, ph, squares


def _thumbnail(slides, pid, slide_id, thumb_size):
    import urllib.request

    t = (slides.presentations().pages().getThumbnail(
        presentationId=pid, pageObjectId=slide_id,
        thumbnailProperties_thumbnailSize=thumb_size,
        thumbnailProperties_mimeType="PNG").execute())
    req = urllib.request.Request(t["contentUrl"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(raw)).convert("RGB")
    return np.asarray(img)


def _runs(mask_1d: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as (start, end_exclusive)."""
    out = []
    in_run = False
    start = 0
    for i, v in enumerate(mask_1d):
        if v and not in_run:
            in_run, start = True, i
        elif not v and in_run:
            in_run = False
            out.append((start, i))
    if in_run:
        out.append((start, len(mask_1d)))
    return out


def _detect_squares(arr: np.ndarray, grid: int):
    """Find the grid of white squares by projecting the white mask onto each axis.
    Returns (H, W, measured) where measured[row][col] = (x0,y0,x1,y1) in pixels.
    """
    H, W, _ = arr.shape
    # Mid-level threshold so anti-aliased edges are split ~symmetrically and don't
    # bias the measured square smaller (which would look like a fake scale error).
    white = (arr[:, :, 0] > 127) & (arr[:, :, 1] > 127) & (arr[:, :, 2] > 127)
    col_bands = [r for r in _runs(white.any(axis=0)) if r[1] - r[0] >= 2]
    row_bands = [r for r in _runs(white.any(axis=1)) if r[1] - r[0] >= 2]
    measured = []
    for (ry0, ry1) in row_bands:
        row_cells = []
        for (rx0, rx1) in col_bands:
            sub = white[ry0:ry1, rx0:rx1]
            ys, xs = np.nonzero(sub)
            if len(xs) == 0:
                row_cells.append(None)
                continue
            row_cells.append((rx0 + xs.min(), ry0 + ys.min(),
                              rx0 + xs.max() + 1, ry0 + ys.max() + 1))
        measured.append(row_cells)
    return H, W, measured, len(col_bands), len(row_bands)


def _fit(expected: np.ndarray, measured: np.ndarray):
    """Least-squares measured = slope*expected + intercept. Returns (slope, intercept, max_abs_resid)."""
    A = np.vstack([expected, np.ones_like(expected)]).T
    (slope, intercept), *_ = np.linalg.lstsq(A, measured, rcond=None)
    resid = measured - (slope * expected + intercept)
    return slope, intercept, float(np.max(np.abs(resid)))


def main():
    ap = argparse.ArgumentParser(description="Calibrate Slides thumbnail geometry vs our overlay math")
    ap.add_argument("--token", type=Path, default=Path("gesture_deck/token_picker.json"))
    ap.add_argument("--thumb-size", default="LARGE")
    ap.add_argument("--grid", type=int, default=4, help="NxN grid of probe squares")
    ap.add_argument("--square-in", type=float, default=0.6, help="probe square side, inches")
    ap.add_argument("--keep", action="store_true", help="don't delete the probe presentation")
    args = ap.parse_args()

    import slides_auth_picker as pk

    creds = pk._load_cached(args.token)
    if not creds:
        raise SystemExit(f"No cached credentials at {args.token}. Run the studio/import once first.")
    slides, drive = _svc(creds)

    print(f"[calibrate] creating probe ({args.grid}x{args.grid} squares)…")
    pid, slide_id, pw, ph, squares = _build_probe(slides, args.grid, args.square_in)
    try:
        arr = _thumbnail(slides, pid, slide_id, args.thumb_size)
        H, W, measured, ncols, nrows = _detect_squares(arr, args.grid)
        print(f"[calibrate] thumbnail {W}x{H}px  (page {pw:.0f}x{ph:.0f} EMU, "
              f"aspect img={W/H:.5f} page={pw/ph:.5f})")
        print(f"[calibrate] detected {ncols} columns x {nrows} rows of squares")

        if ncols != args.grid or nrows != args.grid:
            print("[calibrate] WARNING: detected grid != expected; results may be partial.")

        # Pair measured cells (row-major, sorted) with expected squares (row-major).
        exp_cx, exp_cy, exp_w, exp_h = [], [], [], []
        mea_cx, mea_cy, mea_w, mea_h = [], [], [], []
        k = 0
        for r in range(len(measured)):
            for c in range(len(measured[r])):
                cell = measured[r][c]
                if cell is None or k >= len(squares):
                    k += 1
                    continue
                sqd = squares[k]
                x0, y0, x1, y1 = cell
                exp_cx.append(sqd["fcx"]); exp_cy.append(sqd["fcy"])
                exp_w.append(sqd["fw"]);  exp_h.append(sqd["fh"])
                mea_cx.append(((x0 + x1) / 2) / W); mea_cy.append(((y0 + y1) / 2) / H)
                mea_w.append((x1 - x0) / W);        mea_h.append((y1 - y0) / H)
                k += 1

        for label, e, m in (
            ("X center", exp_cx, mea_cx),
            ("Y center", exp_cy, mea_cy),
            ("Width",    exp_w,  mea_w),
            ("Height",   exp_h,  mea_h),
        ):
            e = np.array(e); m = np.array(m)
            slope, intercept, mr = _fit(e, m)
            # residual in *pixels* for intuition
            scale_px = W if "X" in label or label == "Width" else H
            print(f"\n[{label}]  measured = {slope:.5f}*expected + {intercept:+.5f}")
            print(f"    scale error : {(slope-1)*100:+.3f}%")
            print(f"    offset      : {intercept*scale_px:+.2f}px ({intercept*100:+.3f}% of slide)")
            print(f"    max residual: {mr*scale_px:.2f}px (nonlinearity/noise)")

        print("\n[calibrate] interpretation:")
        print("  slope~=1 & offset~=0px  -> our overlay math already matches Google.")
        print("  offset!=0               -> constant translation to correct.")
        print("  slope!=1                -> a scale factor to apply.")
        print("  large max residual      -> nonlinear (rotation/shear/lens); needs more than affine.")
    finally:
        if not args.keep:
            try:
                drive.files().delete(fileId=pid, supportsAllDrives=True).execute()
                print(f"\n[calibrate] deleted probe {pid}")
            except Exception as e:
                print(f"\n[calibrate] could not delete probe {pid}: {e!r}")
        else:
            print(f"\n[calibrate] kept probe: https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()

"""Generate a reveal.js deck from exported slide images + a manifest.

Each slide (exported from Google Slides, Keynote, PowerPoint, etc.) becomes a
full-bleed background image. Slides listed in the manifest get a live <iframe>
overlay (lazy-loaded via data-src) positioned by percentage rect, pointing at a
locally-served viewer. The deck enables the knobs behind the interaction
contract (preventIframeAutoFocus) and installs a parent-side listener that turns
the shim's postMessages into slide navigation.

Usage:
    python build_deck.py --manifest manifest.json [--out deck.html] [--cdn]

If the manifest specifies "pdf", it is split into per-slide PNGs (requires
PyMuPDF: pip install pymupdf). If it specifies "images", that directory's images
are used in natural sort order.

reveal.js is vendored locally (into vendor/reveal/) by default so the deck has
no CDN dependency; pass --cdn to reference the CDN instead.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import urllib.request
from pathlib import Path

from deckcommon import PACKAGE_DIR, load_manifest, resolve_embeds

REVEAL_VER = "6.0.1"
REVEAL_CDN = f"https://cdn.jsdelivr.net/npm/reveal.js@{REVEAL_VER}"
VENDOR = PACKAGE_DIR / "vendor" / "reveal"


def ensure_reveal(theme: str) -> bool:
    """Download reveal.js assets into vendor/reveal/ (once). Returns True on a
    complete local copy; falls back to CDN if a download fails."""
    rels = ["dist/reset.css", "dist/reveal.css", "dist/reveal.js", f"dist/theme/{theme}.css"]
    for rel in rels:
        dst = VENDOR / rel
        if dst.exists() and dst.stat().st_size > 0:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(f"{REVEAL_CDN}/{rel}", timeout=30) as r:
                dst.write_bytes(r.read())
        except Exception as e:
            print(f"[build] vendor download failed for {rel} ({e}); using CDN")
            return False
    return True


def _natsort_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def split_pdf(pdf_path: Path, out_dir: Path, dpi: int = 150) -> list[Path]:
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise SystemExit(
            "Splitting a PDF needs PyMuPDF (pip install pymupdf), or export "
            "per-slide images and use the manifest \"images\" field instead."
        ) from e

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pages: list[Path] = []
    for i, page in enumerate(doc):
        dst = out_dir / f"slide-{i + 1:03d}.png"
        page.get_pixmap(matrix=mat).save(dst)
        pages.append(dst)
    return pages


def collect_images(manifest: dict) -> list[Path]:
    mdir = Path(manifest["_manifest_dir"])
    if manifest.get("pdf"):
        return split_pdf((mdir / manifest["pdf"]).resolve(), mdir / "slides_png")
    if manifest.get("images"):
        img_dir = (mdir / manifest["images"]).resolve()
        imgs = [p for p in img_dir.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".svg")]
        if not imgs:
            raise SystemExit(f"No images found in {img_dir}")
        return sorted(imgs, key=_natsort_key)
    raise SystemExit('Manifest must set either "pdf" or "images".')


def build_sections(images: list[Path], embeds: list[dict], out_dir: Path) -> str:
    by_slide: dict[int, list[dict]] = {}
    for e in embeds:
        by_slide.setdefault(int(e["slide"]), []).append(e)

    sections = []
    for idx, img in enumerate(images, start=1):
        rel = os.path.relpath(img, out_dir).replace("\\", "/")
        overlays = ""
        for e in by_slide.get(idx, []):
            rect = e.get("rect") or {"x": 0, "y": 0, "w": 100, "h": 100}
            style = (
                f"position:absolute;left:{rect['x']}%;top:{rect['y']}%;"
                f"width:{rect['w']}%;height:{rect['h']}%;"
                "border:0;box-shadow:0 4px 24px rgba(0,0,0,0.5);background:#000;"
            )
            url = html.escape(e["url"], quote=True)
            overlays += (
                f'\n        <iframe class="embed" data-src="{url}" '
                f'style="{style}" allow="autoplay; fullscreen"></iframe>'
            )
        sections.append(
            f'      <section data-background-color="#000" '
            f'data-background-image="{html.escape(rel, quote=True)}" '
            f'data-background-size="contain">{overlays}\n      </section>'
        )
    return "\n".join(sections)


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="{base}/dist/reset.css">
<link rel="stylesheet" href="{base}/dist/reveal.css">
<link rel="stylesheet" href="{base}/dist/theme/{theme}.css">
<style>
  .reveal .slides section {{ height: 100%; }}
  .reveal iframe.embed {{ display: block; }}
</style>
</head>
<body>
<div class="reveal">
  <div class="slides">
{sections}
  </div>
</div>
<script src="{base}/dist/reveal.js"></script>
<script>
  Reveal.initialize({{
    width: {width},
    height: {height},
    margin: 0,
    controls: true,
    progress: true,
    hash: true,
    keyboard: true,
    // Do not let a clicked-into viewer swallow the keyboard on load.
    preventIframeAutoFocus: true,
    // Lazy-load iframes only when their slide is near.
    preloadIframes: null,
    viewDistance: 1
  }});

  // Parent side of the interaction contract: the shim inside each viewer posts
  // {{type:'deck-nav', dir}} on Arrow Left/Right. Everything else stays local.
  window.addEventListener('message', function (e) {{
    var d = e.data;
    if (d && d.type === 'deck-nav') {{
      if (d.dir === 'right') Reveal.right();
      else if (d.dir === 'left') Reveal.left();
    }}
  }});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Build reveal.js deck from slides + manifest")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--cdn", action="store_true", help="Force reveal.js from CDN (skip local vendoring)")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)
    out = (args.out or (Path(manifest["_manifest_dir"]) / "deck.html")).resolve()
    out_dir = out.parent
    theme = manifest.get("reveal_theme", "black")

    if args.cdn or not ensure_reveal(theme):
        base = REVEAL_CDN
    else:
        base = os.path.relpath(VENDOR, out_dir).replace("\\", "/")

    images = collect_images(manifest)
    embeds = resolve_embeds(manifest)
    sections = build_sections(images, embeds, out_dir)

    doc = TEMPLATE.format(
        title=html.escape(manifest.get("title", "Deck")),
        base=base,
        theme=theme,
        sections=sections,
        width=int(manifest.get("width", 1280)),
        height=int(manifest.get("height", 720)),
    )
    out.write_text(doc, encoding="utf-8")

    print(f"[build] reveal.js source: {base}")
    print(f"[build] {len(images)} slides, {len(embeds)} embed(s) -> {out}")
    for e in embeds:
        print(f"        slide {e['slide']}: {e['serve']:6s} {e['url']}")


if __name__ == "__main__":
    main()

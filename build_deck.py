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
from urllib.parse import urlencode

from deckcommon import PACKAGE_DIR, STATIC_PORT, load_manifest, resolve_embeds

# Minimal scaler served next to the deck. An overlay that wants "design-viewport"
# fitting points a normal percent-sized iframe at this page; the page lays the
# real site out at a fixed design resolution (where it looks good, no scrollbars)
# and CSS-scales that whole iframe to fill its container. Because this runs in a
# plain browsing context (not inside reveal's own scaled .slides), the transform
# behaves predictably. Query params: src, dw, dh, fit(fill|contain), transparent.
_SCALER_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;overflow:hidden;background:#000}
  #f{position:absolute;border:0;transform-origin:top left;left:0;top:0}
</style></head><body><iframe id="f" scrolling="no" allow="autoplay; fullscreen"></iframe>
<script>
  var q=new URLSearchParams(location.search);
  var src=q.get('src');
  var dw=+(q.get('dw')||1280), dh=+(q.get('dh')||720);
  var fit=q.get('fit')||'contain';
  var mW=q.get('w'), mH=q.get('h');
  if(q.get('transparent')==='1') document.body.style.background='transparent';
  var f=document.getElementById('f');
  f.style.width=dw+'px'; f.style.height=dh+'px';
  if(src) f.src=src;
  function layout(){
    var W=mW?+mW:window.innerWidth, H=mH?+mH:window.innerHeight;
    var sx=W/dw, sy=H/dh;
    if(fit==='contain'){ sx=sy=Math.min(sx,sy); }
    f.style.left=((W-dw*sx)/2)+'px';
    f.style.top=((H-dh*sy)/2)+'px';
    f.style.transform='scale('+sx+(fit==='fill'?','+sy:'')+')';
  }
  layout(); window.addEventListener('resize', layout);
</script></body></html>
"""


def _scaler_url(embed_url: str, design: dict, transparent: bool, relative: bool) -> str:
    """URL of the scaler page wrapping embed_url at the given design viewport."""
    base = "/_scale.html" if relative else f"http://localhost:{STATIC_PORT}/_scale.html"
    params = {
        "src": embed_url,
        "dw": int(design.get("w", 1280)),
        "dh": int(design.get("h", 720)),
        "fit": design.get("fit", "contain"),
    }
    if transparent:
        params["transparent"] = "1"
    return base + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Website slides: a first-class, re-import-surviving way to drop a live website
# in as its OWN standalone slide, inserted after a given frame. Configured in a
# plain text file (website_slides.conf) next to the manifest, one embed per line:
#
#   frame | url | scale | dx | dy | designWxH
#
#   frame     : slide number to insert AFTER (1-based). 0 = before slide 1.
#   url        : https://...  OR a deck-relative path (e.g. dvq/dvq_viewer.html)
#   scale      : uniform multiplier over the "fit-to-slide" (contain) baseline.
#                1.0 = as large as fits preserving aspect; 0.5 = half that. (default 1.0)
#   dx, dy     : offset of the embed CENTER from slide CENTER, as a FRACTION of
#                slide size (0,0 = centered; +x right, +y down). (default 0)
#   designWxH  : logical resolution the site lays out at, e.g. 1600x900. Defines
#                the aspect used for uniform scaling. (default 1600x900)
#
# The site is always centered by default and scaled uniformly (never distorted),
# rendered through the same _scale.html design-viewport scaler as design embeds.
# ---------------------------------------------------------------------------
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_DESIGN_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")


def _is_url(s: str) -> bool:
    return bool(_URL_SCHEME_RE.match(s))


def parse_website_slides(conf_path: Path) -> list[dict]:
    """Parse website_slides.conf into a list of embed dicts. Missing file -> []."""
    slides: list[dict] = []
    if not conf_path.exists():
        return slides
    name = conf_path.name
    for lineno, raw in enumerate(conf_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[1]:
            print(f"[build] {name}:{lineno}: need at least 'frame | url'; skipping")
            continue
        try:
            after = int(float(parts[0]))
        except ValueError:
            print(f"[build] {name}:{lineno}: bad frame {parts[0]!r}; skipping")
            continue

        def _num(i, default):
            return float(parts[i]) if len(parts) > i and parts[i] else default

        try:
            scale = _num(2, 1.0)
            dx = _num(3, 0.0)
            dy = _num(4, 0.0)
        except ValueError:
            print(f"[build] {name}:{lineno}: bad scale/dx/dy; skipping")
            continue

        dw, dh = 1600, 900
        if len(parts) > 5 and parts[5]:
            m = _DESIGN_RE.match(parts[5])
            if m:
                dw, dh = int(m.group(1)), int(m.group(2))
            else:
                print(f"[build] {name}:{lineno}: bad designWxH {parts[5]!r}; using 1600x900")

        slides.append({"after": after, "url": parts[1], "scale": scale,
                       "dx": dx, "dy": dy, "dw": dw, "dh": dh})
    return slides


def _website_rect(scale: float, dx: float, dy: float,
                  dw: int, dh: int, deck_w: int, deck_h: int) -> dict:
    """Percent rect (of the slide) for a uniformly-scaled, centered+offset embed.

    Baseline is a contain-fit of the design (dw x dh) into the slide; `scale`
    multiplies that, then (dx, dy) offset the center. The resulting box has the
    exact design aspect, so the scaler's contain-fit fills it with no letterbox.
    """
    slide_aspect = deck_w / deck_h
    design_aspect = dw / dh
    if design_aspect >= slide_aspect:
        base_w, base_h = 1.0, slide_aspect / design_aspect
    else:
        base_w, base_h = design_aspect / slide_aspect, 1.0
    wf, hf = base_w * scale, base_h * scale
    xf = (1.0 - wf) / 2.0 + dx
    yf = (1.0 - hf) / 2.0 + dy
    return {"x": xf * 100, "y": yf * 100, "w": wf * 100, "h": hf * 100}


def _website_section(w: dict, deck_w: int, deck_h: int, relative: bool) -> str:
    """A standalone black slide holding one uniformly-scaled website embed."""
    if _is_url(w["url"]):
        embed_url = w["url"]
    else:
        path = w["url"].lstrip("/")
        embed_url = ("/" + path) if relative else f"http://localhost:{STATIC_PORT}/{path}"
    design = {"w": w["dw"], "h": w["dh"], "fit": "contain"}
    scaler = _scaler_url(embed_url, design, transparent=False, relative=relative)
    rect = _website_rect(w["scale"], w["dx"], w["dy"], w["dw"], w["dh"], deck_w, deck_h)
    style = (
        f"position:absolute;left:{rect['x']}%;top:{rect['y']}%;"
        f"width:{rect['w']}%;height:{rect['h']}%;border:0;background:#000;"
    )
    iframe = (
        f'\n        <iframe class="embed" data-src="{html.escape(scaler, quote=True)}" '
        f'style="{style}" allow="autoplay; fullscreen"></iframe>'
    )
    return f'      <section data-background-color="#000">{iframe}\n      </section>'


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


def build_sections(images: list[Path], embeds: list[dict], out_dir: Path,
                   deck_w: int = 1280, deck_h: int = 720,
                   website_slides: list[dict] | None = None,
                   relative: bool = False) -> str:
    by_slide: dict[int, list[dict]] = {}
    for e in embeds:
        by_slide.setdefault(int(e["slide"]), []).append(e)

    # Website slides are inserted AFTER a given frame; group them by that frame.
    ws_after: dict[int, list[dict]] = {}
    for w in (website_slides or []):
        ws_after.setdefault(int(w["after"]), []).append(w)

    sections = []

    def emit_websites(after_idx: int):
        for w in ws_after.get(after_idx, []):
            sections.append(_website_section(w, deck_w, deck_h, relative))

    emit_websites(0)  # allow inserting a website slide before slide 1
    for idx, img in enumerate(images, start=1):
        rel = os.path.relpath(img, out_dir).replace("\\", "/")
        overlays = ""
        for e in by_slide.get(idx, []):
            # relative=True: same-origin root-relative URL (studio server serves
            # the viewer from the same host as the deck). Otherwise the absolute
            # localhost:PORT URL computed by resolve_embeds (launch.py flow).
            embed_url = ("/" + e.get("path", "").lstrip("/")) if relative else e["url"]
            rect = e.get("rect") or {"x": 0, "y": 0, "w": 100, "h": 100}
            transparent = bool(e.get("transparent"))
            # An overlay with "design" is fitted at a fixed layout viewport via
            # the scaler page; the deck-side iframe stays a plain percent box that
            # reveal handles natively (a second transform inside reveal's own
            # scaled .slides misbehaves).
            if e.get("design"):
                embed_url = _scaler_url(embed_url, e["design"], transparent, relative)
            # Transparent overlays (e.g. images/GIFs with alpha) let the slide
            # background show through; opaque ones get a black backing + shadow.
            if transparent:
                backing = "background:transparent;"
            else:
                backing = "box-shadow:0 4px 24px rgba(0,0,0,0.5);background:#000;"
            style = (
                f"position:absolute;left:{rect['x']}%;top:{rect['y']}%;"
                f"width:{rect['w']}%;height:{rect['h']}%;border:0;" + backing
            )
            url = html.escape(embed_url, quote=True)
            transparency = ' allowtransparency="true"' if transparent else ""
            overlays += (
                f'\n        <iframe class="embed" data-src="{url}" '
                f'style="{style}" allow="autoplay; fullscreen"{transparency}></iframe>'
            )
        sections.append(
            f'      <section data-background-color="#000" '
            f'data-background-image="{html.escape(rel, quote=True)}" '
            f'data-background-size="contain">{overlays}\n      </section>'
        )
        emit_websites(idx)
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
    controls: false,
    progress: false,
    transition: 'none',
    backgroundTransition: 'none',
    hash: true,
    keyboard: true,
    // Do not let a clicked-into viewer swallow the keyboard on load.
    preventIframeAutoFocus: true,
    // Snappiness: eagerly load iframes (and background images) for slides within
    // viewDistance of the current one, so navigating to the next 2 slides shows
    // already-loaded media with no request/decode hitch. reveal preloads
    // background images within the same window automatically.
    preloadIframes: true,
    viewDistance: 2
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

  // Arrow keys ALWAYS change slides, even when focus is inside a viewer. When an
  // iframe has focus the parent never sees keydown, so we reach into each
  // same-origin iframe's document and forward Arrow/PageUp-Down to the deck,
  // swallowing them (capture + stopImmediatePropagation) so the viewer can't
  // also act on them. Cross-origin iframes throw on access and fall back to the
  // postMessage shim above.
  function onArrowKey(ev) {{
    if (ev.key === 'ArrowRight' || ev.key === 'PageDown') {{
      ev.preventDefault(); ev.stopImmediatePropagation(); Reveal.right();
    }} else if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') {{
      ev.preventDefault(); ev.stopImmediatePropagation(); Reveal.left();
    }}
  }}
  function bindArrows(f) {{
    try {{
      var doc = f.contentDocument || (f.contentWindow && f.contentWindow.document);
      if (!doc) return;
      if (!doc.__deckArrows) {{
        doc.__deckArrows = true;
        doc.addEventListener('keydown', onArrowKey, true);
      }}
      // Recurse into nested same-origin iframes (e.g. the scaler's inner site),
      // so arrows work no matter how deep focus goes.
      doc.querySelectorAll('iframe').forEach(function (k) {{
        if (k.__deckBound) return;
        k.__deckBound = true;
        k.addEventListener('load', function () {{ bindArrows(k); }});
        bindArrows(k);
      }});
    }} catch (err) {{ /* cross-origin viewer: relies on the shim instead */ }}
  }}
  function bindAllEmbeds() {{
    document.querySelectorAll('iframe.embed').forEach(function (f) {{
      if (f.__deckBound) return;
      f.__deckBound = true;
      f.addEventListener('load', function () {{ bindArrows(f); }});
      bindArrows(f);  // in case it is already loaded
    }});
  }}
  Reveal.on('ready', bindAllEmbeds);
  Reveal.on('slidechanged', bindAllEmbeds);
</script>
</body>
</html>
"""


def build(
    manifest_path: Path,
    out: Path | None = None,
    cdn: bool = False,
    relative_embeds: bool = False,
    reveal_base: str | None = None,
    quiet: bool = False,
) -> Path:
    """Build deck.html from a manifest. Returns the deck path.

    - relative_embeds: emit same-origin root-relative viewer URLs (studio server).
    - reveal_base: explicit URL/path for the vendored reveal.js assets (e.g.
      "/vendor/reveal" when a studio server serves them from the same origin).
    """
    manifest = load_manifest(manifest_path)
    out = (out or (Path(manifest["_manifest_dir"]) / "deck.html")).resolve()
    out_dir = out.parent
    theme = manifest.get("reveal_theme", "black")

    if reveal_base is not None:
        ensure_reveal(theme)  # make sure the assets exist to be served
        base = reveal_base
    elif cdn or not ensure_reveal(theme):
        base = REVEAL_CDN
    else:
        base = os.path.relpath(VENDOR, out_dir).replace("\\", "/")

    deck_w = int(manifest.get("width", 1280))
    deck_h = int(manifest.get("height", 720))

    images = collect_images(manifest)
    embeds = resolve_embeds(manifest)
    # First-class website slides live in a plain-text file next to the manifest
    # so they survive a re-import (which rewrites manifest.json wholesale).
    website_slides = parse_website_slides(out_dir / "website_slides.conf")
    sections = build_sections(images, embeds, out_dir, deck_w, deck_h,
                              website_slides, relative=relative_embeds)

    # Ship the scaler page next to the deck if anything uses design-viewport
    # fitting (design overlays or website slides).
    if any(e.get("design") for e in embeds) or website_slides:
        (out_dir / "_scale.html").write_text(_SCALER_HTML, encoding="utf-8")

    doc = TEMPLATE.format(
        title=html.escape(manifest.get("title", "Deck")),
        base=base,
        theme=theme,
        sections=sections,
        width=deck_w,
        height=deck_h,
    )
    out.write_text(doc, encoding="utf-8")

    if not quiet:
        print(f"[build] reveal.js source: {base}")
        print(f"[build] {len(images)} slides, {len(embeds)} embed(s), "
              f"{len(website_slides)} website slide(s) -> {out}")
        for e in embeds:
            print(f"        slide {e['slide']}: {e['serve']:6s} {e['url']}")
        for w in website_slides:
            print(f"        after {w['after']}: website  {w['url']} "
                  f"(scale {w['scale']}, off {w['dx']},{w['dy']}, {w['dw']}x{w['dh']})")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build reveal.js deck from slides + manifest")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--cdn", action="store_true", help="Force reveal.js from CDN (skip local vendoring)")
    ap.add_argument("--relative-embeds", action="store_true",
                    help="Emit same-origin root-relative viewer URLs (studio server)")
    ap.add_argument("--reveal-base", default=None,
                    help="Explicit base URL/path for reveal.js assets (e.g. /vendor/reveal)")
    args = ap.parse_args()
    build(
        args.manifest,
        out=args.out,
        cdn=args.cdn,
        relative_embeds=args.relative_embeds,
        reveal_base=args.reveal_base,
    )


if __name__ == "__main__":
    main()

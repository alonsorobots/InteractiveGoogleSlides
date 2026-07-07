"""Import a Google Slides deck via the API, keeping media *live* instead of baked.

The PDF/thumbnail path flattens everything: an animated GIF becomes one frame, a
video becomes its poster still. This tool takes the privileged path instead. It
authenticates to the Google Slides API, reads the deck as structured JSON, and
for every media element (image/GIF, YouTube video, Drive video) it:

  1. records the element's exact on-slide rectangle (from its affine transform +
     size, composed through any parent groups), and
  2. emits a live overlay for it - a real <img>/<iframe> served with the deck's
     arrow-key shim - positioned by that rectangle.

The static design of each slide is still rendered to a background PNG (via the
Slides `getThumbnail` endpoint, server-side, at up to 1600px), so text, shapes
and layout look identical to Slides. The difference is that the media rectangles
now carry a live element on top of the flat poster, so GIFs animate and videos
play inside the deck.

The output is a plain manifest.json + asset folders that the existing
`build_deck.py` / `launch.py` consume unchanged. This runs *parallel* to the
PDF/images flow; nothing about that flow changes.

Auth (see IMPORT.md for setup): two models, auto-detected.
  - service account : a static JSON key on this machine, no interactive login
    ever. Share the deck (or a folder) with the service account's email.
    Provide via --sa or GOOGLE_APPLICATION_CREDENTIALS.
  - OAuth (installed app) : one browser login, then a cached refresh token in
    token.json refreshes silently forever (publish the consent screen to avoid
    the 7-day testing-mode expiry). Provide the client secret via --client.

Usage:
    python slides_import.py --url "<slides url or id>" --out mydeck --build

Requires: google-api-python-client, google-auth, google-auth-oauthlib
    pip install -r requirements.txt        # (uncomment the google-* lines)
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def _ensure_user_site() -> None:
    """Re-exec once with user site-packages enabled if the Google libs are only
    installed there but disabled (PYTHONNOUSERSITE=1 or python -s).

    On some setups `pip install --user` puts the packages in the user site, yet
    the environment disables it, so `import googleapiclient` fails even though it
    is installed. Rather than make the user prefix every run with
    `PYTHONNOUSERSITE=`, we transparently relaunch with it cleared.
    """
    if importlib.util.find_spec("googleapiclient") is not None:
        return
    if os.environ.get("_SLIDES_IMPORT_REEXEC") == "1":
        return  # already retried; let the normal missing-deps error surface
    if os.environ.get("PYTHONNOUSERSITE") or sys.flags.no_user_site:
        import subprocess

        env = dict(os.environ)
        env.pop("PYTHONNOUSERSITE", None)
        env["_SLIDES_IMPORT_REEXEC"] = "1"
        # Relaunch with user-site enabled (subprocess, since os.execve is
        # unreliable on Windows). The child drops any -s flag.
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env=env
        )
        sys.exit(proc.returncode)


_ensure_user_site()

# Least-privilege, read-only scopes. We request only what a given run needs:
#   - presentations.readonly : read the deck + render slide thumbnails (always).
#   - drive.metadata.readonly : search Drive by NAME to resolve --name to an ID
#     (metadata only - this does NOT grant access to file *contents*). Requested
#     only when --name is used; skipped entirely when you pass a URL/ID.
SCOPE_SLIDES = "https://www.googleapis.com/auth/presentations.readonly"
SCOPE_DRIVE_SEARCH = "https://www.googleapis.com/auth/drive.metadata.readonly"


def _scopes(need_drive_search: bool) -> list[str]:
    return [SCOPE_SLIDES] + ([SCOPE_DRIVE_SEARCH] if need_drive_search else [])

EMU_PER_PT = 12700
EMU_PER_INCH = 914400
PX_PER_INCH = 96

_ID_RE = re.compile(r"/presentation/d/([a-zA-Z0-9_-]+)")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _missing_deps(exc: Exception):
    raise SystemExit(
        "The Google API client libraries are required for slides_import.py.\n"
        "Install them with:\n"
        "    pip install google-api-python-client google-auth google-auth-oauthlib\n"
        f"(import error: {exc})"
    )


def _discover_client(out: Path) -> Path | None:
    """Find an OAuth client secret without requiring --client, so the everyday
    command stays short. Looks in the output dir, then next to this script, then
    the current directory."""
    here = Path(__file__).resolve().parent
    for base in (out, here, Path.cwd()):
        hits = sorted(base.glob("client_secret*.json")) + sorted(base.glob("client_secrets.json"))
        if hits:
            print(f"[import] using OAuth client {hits[0]}")
            return hits[0]
    return None


def get_credentials(sa_path: Path | None, client_path: Path | None, token_path: Path, scopes: list[str]):
    """Return API credentials, auto-selecting the auth model.

    Precedence: explicit --sa, then GOOGLE_APPLICATION_CREDENTIALS, then an
    OAuth client secret (--client). A cached OAuth token.json is reused and
    silently refreshed when present.
    """
    try:
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as e:  # pragma: no cover - environment dependent
        _missing_deps(e)

    env_sa = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    sa = sa_path or (Path(env_sa) if env_sa else None)

    if sa and sa.exists():
        print(f"[import] auth: service account ({sa})")
        return service_account.Credentials.from_service_account_file(str(sa), scopes=scopes)

    # OAuth installed-app flow with a cached, auto-refreshing token. If the cached
    # token was granted a different scope set, ignore it and re-consent.
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        if creds and creds.scopes and not set(scopes).issubset(set(creds.scopes)):
            print("[import] cached token has different scopes; re-consenting")
            creds = None

    if creds and creds.valid:
        print(f"[import] auth: OAuth (cached token {token_path})")
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f"[import] auth: OAuth (refreshed token {token_path})")
        return creds

    if client_path and client_path.exists():
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as e:  # pragma: no cover
            _missing_deps(e)
        print(f"[import] auth: OAuth first-time login using {client_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f"[import] auth: OAuth token cached to {token_path}")
        return creds

    raise SystemExit(
        "No usable credentials found. Provide one of:\n"
        "  --sa service_account.json        (or set GOOGLE_APPLICATION_CREDENTIALS)\n"
        "  --client client_secret.json      (OAuth; a browser opens once)\n"
        "See IMPORT.md for the Google Cloud setup."
    )


# --------------------------------------------------------------------------- #
# Geometry: affine transforms, composed through parent groups
# --------------------------------------------------------------------------- #
# An affine transform is stored as (a, b, c, d, e, f) meaning:
#   x' = a*x + b*y + e
#   y' = c*x + d*y + f
def _emu(magnitude: float, unit: str | None) -> float:
    if unit == "PT":
        return magnitude * EMU_PER_PT
    return magnitude  # EMU or unspecified


def _affine_of(transform: dict | None) -> tuple:
    if not transform:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    unit = transform.get("unit")
    return (
        float(transform.get("scaleX", 1.0)),
        float(transform.get("shearX", 0.0)),
        float(transform.get("shearY", 0.0)),
        float(transform.get("scaleY", 1.0)),
        _emu(float(transform.get("translateX", 0.0)), unit),
        _emu(float(transform.get("translateY", 0.0)), unit),
    )


def _compose(p: tuple, c: tuple) -> tuple:
    """Return p ∘ c (apply c first, then the parent p)."""
    pa, pb, pc, pd, pe, pf = p
    ca, cb, cc, cd, ce, cf = c
    return (
        pa * ca + pb * cc,
        pa * cb + pb * cd,
        pc * ca + pd * cc,
        pc * cb + pd * cd,
        pa * ce + pb * cf + pe,
        pc * ce + pd * cf + pf,
    )


def _rect_percent(abs_xf: tuple, size: dict, page_w: float, page_h: float) -> dict:
    """Convert an element's absolute transform + intrinsic size to a percent box.

    Rotation is handled via the linear part's vector lengths; shear is ignored
    (the Slides API itself warns sizing is approximate under shear).
    """
    a, b, c, d, e, f = abs_xf
    w_emu = _emu(float(size["width"]["magnitude"]), size["width"].get("unit"))
    h_emu = _emu(float(size["height"]["magnitude"]), size["height"].get("unit"))
    rendered_w = w_emu * math.hypot(a, c)
    rendered_h = h_emu * math.hypot(b, d)
    return {
        "x": round(e / page_w * 100, 3),
        "y": round(f / page_h * 100, 3),
        "w": round(rendered_w / page_w * 100, 3),
        "h": round(rendered_h / page_h * 100, 3),
    }


# --------------------------------------------------------------------------- #
# Walk the deck
# --------------------------------------------------------------------------- #
SLIDES_MIME = "application/vnd.google-apps.presentation"


def extract_id(url_or_id: str) -> str:
    m = _ID_RE.search(url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url_or_id):
        return url_or_id
    raise SystemExit(f"Could not extract a presentation ID from: {url_or_id!r}")


def find_by_name(creds, name: str) -> str:
    """Look up a presentation ID by (partial) title in the user's Drive.

    Needs Drive access; a service account only sees decks shared with it.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover
        _missing_deps(e)

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    safe = name.replace("'", "\\'")
    q = f"mimeType='{SLIDES_MIME}' and name contains '{safe}' and trashed=false"
    resp = (
        drive.files()
        .list(
            q=q,
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=25,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files", [])
    if not files:
        raise SystemExit(
            f"No Google Slides deck matching {name!r} found.\n"
            "  - With a service account, share the deck with its email first.\n"
            "  - Or pass the deck URL/ID with --url."
        )
    exact = [f for f in files if f["name"].lower() == name.lower()]
    chosen = exact[0] if len(exact) == 1 else (files[0] if len(files) == 1 else None)
    if chosen is None:
        lines = "\n".join(f"    {f['name']}  ->  {f['id']}" for f in files)
        raise SystemExit(
            f"{len(files)} decks match {name!r}; pass one explicitly with --url <id>:\n{lines}"
        )
    print(f"[import] resolved {name!r} -> {chosen['name']} ({chosen['id']})")
    return chosen["id"]


def _iter_media(elements: list[dict], parent_xf: tuple):
    """Yield (kind, element, absolute_transform) for every image/video, recursing
    into groups so nested media get correct absolute placement."""
    for el in elements:
        xf = _compose(parent_xf, _affine_of(el.get("transform")))
        if "image" in el:
            yield "image", el, xf
        elif "video" in el:
            yield "video", el, xf
        elif "elementGroup" in el:
            yield from _iter_media(el["elementGroup"].get("children", []), xf)


def _fetch(url: str) -> tuple[bytes, str]:
    """Return (bytes, content_type) for a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (slides-import)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), (r.headers.get("Content-Type") or "")


def _sniff(url: str) -> tuple[bytes, str]:
    """Return (first bytes, content_type) without downloading the whole file.

    Used to tell an animated GIF from a static image cheaply: static images stay
    baked into the slide background (no overlay, no download), so we only pull the
    full bytes for the GIFs we actually overlay."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (slides-import)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(64), (r.headers.get("Content-Type") or "")


def _is_gif(head: bytes, ctype: str, url: str) -> bool:
    if head[:4] == b"GIF8":
        return True
    if head[:4] in (b"\x89PNG", b"\xff\xd8\xff") or head[:2] == b"\xff\xd8":
        return False
    if "gif" in ctype.lower():
        return True
    return bool(re.search(r"\.gif(?:$|[?#])", url, re.I))


def _download(url: str, dst: Path) -> None:
    data, _ = _fetch(url)
    dst.write_bytes(data)


_CTYPE_EXT = {
    "image/gif": "gif",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


def _real_ext(data: bytes, ctype: str, url: str) -> str:
    """Pick a file extension from magic bytes first (most reliable), then the
    HTTP Content-Type, then the URL, defaulting to png.

    Google's image contentUrls carry no extension, so guessing from the URL
    alone silently mislabels animated GIFs as PNGs."""
    if data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    ct = ctype.split(";")[0].strip().lower()
    if ct in _CTYPE_EXT:
        return _CTYPE_EXT[ct]
    m = re.search(r"\.(png|jpe?g|gif|webp|svg)(?:$|[?#])", url, re.I)
    return m.group(1).lower().replace("jpeg", "jpg") if m else "png"


# --------------------------------------------------------------------------- #
# Overlay wrappers (served static, so the arrow-key shim is injected)
# --------------------------------------------------------------------------- #
_WRAP = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  html,body{{margin:0;height:100%;background:{bg};overflow:hidden}}
  .fill{{position:absolute;inset:0;width:100%;height:100%;border:0}}
  img.fill,video.fill{{object-fit:contain;background:transparent}}
</style></head><body>
{body}
</body></html>
"""


def _img_wrapper(media_rel: str) -> str:
    # Transparent background so a PNG/GIF with alpha shows the slide behind it.
    return _WRAP.format(
        bg="transparent",
        body=f'<img class="fill" src="/{html.escape(media_rel)}" alt="">',
    )


def _video_wrapper(media_rel: str, transparent: bool = False) -> str:
    # Autoplaying, muted, looping video behaves like an animated GIF but decodes
    # in hardware. Transparent (alpha WebM) shows the slide through; opaque MP4
    # sits on black.
    return _WRAP.format(
        bg="transparent" if transparent else "#000",
        body=(
            f'<video class="fill" src="/{html.escape(media_rel)}" '
            f'autoplay muted loop playsinline preload="auto"></video>'
        ),
    )


_FFMPEG = shutil.which("ffmpeg")


def _gif_has_alpha(path: Path) -> bool:
    """True if the GIF declares/uses transparency (so we must keep an alpha
    channel). Conservative: any uncertainty -> treat as alpha so nothing turns
    into a black box."""
    try:
        from PIL import Image
        im = Image.open(path)
        if im.mode in ("RGBA", "LA", "PA"):
            return True
        return "transparency" in im.info
    except Exception:
        return True


def _transcode_gif(gif_path: Path):
    """Transcode an animated GIF to a small, hardware-decoded looping video.

    Opaque -> H.264 MP4 (broadest support, GPU decode). Alpha -> VP9 WebM with
    a real alpha channel (Chromium plays this transparently). Returns
    (out_path, transparent) or None if ffmpeg is missing / the encode fails, in
    which case the caller keeps the original GIF.
    """
    if not _FFMPEG:
        return None
    transparent = _gif_has_alpha(gif_path)
    try:
        if transparent:
            out = gif_path.with_suffix(".webm")
            cmd = [_FFMPEG, "-y", "-loglevel", "error", "-i", str(gif_path),
                   "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0",
                   "-crf", "32", "-deadline", "good", "-cpu-used", "5",
                   "-an", str(out)]
        else:
            out = gif_path.with_suffix(".mp4")
            cmd = [_FFMPEG, "-y", "-loglevel", "error", "-i", str(gif_path),
                   "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-an", str(out)]
        subprocess.run(cmd, check=True)
        if out.exists() and out.stat().st_size > 0:
            return out, transparent
    except Exception:
        pass
    try:
        # A half-written output is worse than none.
        for suf in (".mp4", ".webm"):
            p = gif_path.with_suffix(suf)
            if p.exists():
                p.unlink()
    except OSError:
        pass
    return None


def _youtube_wrapper(video_id: str) -> str:
    src = f"https://www.youtube.com/embed/{html.escape(video_id)}?rel=0"
    return _WRAP.format(
        bg="#000",
        body=f'<iframe class="fill" src="{src}" allow="autoplay; fullscreen" '
        f'allowfullscreen></iframe>',
    )


def _drive_wrapper(video_id: str) -> str:
    src = f"https://drive.google.com/file/d/{html.escape(video_id)}/preview"
    return _WRAP.format(
        bg="#000",
        body=f'<iframe class="fill" src="{src}" allow="autoplay; fullscreen" '
        f'allowfullscreen></iframe>',
    )


# --------------------------------------------------------------------------- #
# Main import
# --------------------------------------------------------------------------- #
def perform_import(
    creds,
    pres_id: str,
    out: Path,
    theme: str = "black",
    thumb_size: str = "LARGE",
    manifest_path: Path | None = None,
    progress=None,
) -> Path:
    """Fetch the deck, render clean backgrounds, extract live media, write a manifest.

    Static images stay baked into the slide background (pixel-perfect, no overlay).
    Only GIFs and videos become live overlays - and those elements are removed from
    a throwaway copy of the deck before rendering the backgrounds, so there is no
    duplicate frozen copy showing underneath the live one.

    `progress(stage, current, total, message)` is called (if given) so a UI can
    show conversion progress. Returns the manifest path.
    """
    from googleapiclient.discovery import build as _gbuild

    slides = _gbuild("slides", "v1", credentials=creds, cache_discovery=False)
    drive = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)

    def _p(stage, cur, total, msg):
        if progress:
            progress(stage, cur, total, msg)

    # Start each import from a clean slate so removed slides/elements don't linger
    # as stale files (we overwrite, but a shrunk deck would otherwise leave orphans).
    import shutil

    bg_dir = out / "slides_png"
    media_dir = out / "media"
    wrap_dir = out / "_media"
    for d in (bg_dir, media_dir, wrap_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    for d in (out, bg_dir, media_dir, wrap_dir):
        d.mkdir(parents=True, exist_ok=True)

    _p("fetch", 0, 1, "Fetching presentation…")
    print(f"[import] fetching presentation {pres_id}")
    pres = slides.presentations().get(presentationId=pres_id).execute()

    page_size = pres["pageSize"]
    page_w = _emu(float(page_size["width"]["magnitude"]), page_size["width"].get("unit"))
    page_h = _emu(float(page_size["height"]["magnitude"]), page_size["height"].get("unit"))
    width_px = round(page_w / EMU_PER_INCH * PX_PER_INCH)
    height_px = round(page_h / EMU_PER_INCH * PX_PER_INCH)

    deck_slides = pres.get("slides", [])
    total = len(deck_slides)
    print(f"[import] {total} slides, page {width_px}x{height_px}px")
    _p("analyze", 0, total, f"Converting {total} slides…")

    embeds: list[dict] = []
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    # Per slide (1-based), the ordinal positions (within _iter_media order) of the
    # elements we overlay - used to delete the very same elements from the copy.
    delete_positions: dict[int, list[int]] = {}

    for idx, slide in enumerate(deck_slides, start=1):
        n_media = 0
        for pos, (kind, el, xf) in enumerate(_iter_media(slide.get("pageElements", []), identity)):
            size = el.get("size")
            if not size:
                continue
            rect = _rect_percent(xf, size, page_w, page_h)
            el_id = re.sub(r"[^A-Za-z0-9_.-]", "_", el.get("objectId", f"el{pos}"))
            wrap_name = f"slide{idx:03d}_{el_id}.html"

            if kind == "image":
                url = el["image"].get("contentUrl") or el["image"].get("sourceUrl")
                if not url:
                    continue
                # Only animated GIFs need a live overlay. Everything else stays
                # baked into Google's own slide render (the background), which is
                # always faithful - so if detection is ever unsure, we fall back to
                # "static" and the image still shows correctly, just not animated.
                try:
                    head, ctype = _sniff(url)
                    is_gif = _is_gif(head, ctype, url)
                except Exception:
                    is_gif = False
                if not is_gif:
                    continue  # static image: leave it baked in the background
                data, ctype2 = _fetch(url)
                ext = _real_ext(data, ctype2, url)
                media_name = f"slide{idx:03d}_{el_id}.{ext}"
                gif_path = media_dir / media_name
                gif_path.write_bytes(data)
                # Big win: transcode the (often multi-MB) GIF into a small,
                # GPU-decoded looping video. Falls back to the raw GIF if ffmpeg
                # is unavailable or the encode fails.
                tr = _transcode_gif(gif_path)
                if tr:
                    vpath, transp = tr
                    try:
                        gif_path.unlink()
                    except OSError:
                        pass
                    media_name = vpath.name
                    (wrap_dir / wrap_name).write_text(
                        _video_wrapper(f"media/{media_name}", transp), encoding="utf-8"
                    )
                else:
                    (wrap_dir / wrap_name).write_text(
                        _img_wrapper(f"media/{media_name}"), encoding="utf-8"
                    )
            else:  # video
                vid = el["video"]
                source = vid.get("source", "").upper()
                video_id = vid.get("id")
                if not video_id:
                    continue
                if source == "YOUTUBE":
                    content = _youtube_wrapper(video_id)
                elif source == "DRIVE":
                    content = _drive_wrapper(video_id)
                else:
                    print(f"[import]   slide {idx}: unknown video source {source!r}, skipping")
                    continue
                (wrap_dir / wrap_name).write_text(content, encoding="utf-8")

            embeds.append(
                {
                    "slide": idx,
                    "serve": "static",
                    "path": f"_media/{wrap_name}",
                    "rect": rect,
                    # GIFs may have alpha - let the slide show through. Videos stay
                    # on an opaque black backing.
                    "transparent": kind == "image",
                    "_kind": kind,
                }
            )
            delete_positions.setdefault(idx, []).append(pos)
            n_media += 1

        _p("analyze", idx, total, f"Analyzing slide {idx} of {total}")

    # Render backgrounds from a throwaway copy that has the overlaid elements
    # removed, so no frozen duplicate shows underneath the live overlay.
    _p("render", 0, total, "Preparing clean backgrounds…")
    thumb_pres_id, copy_slides, copy_id = _prepare_background_source(
        slides, drive, pres_id, deck_slides, delete_positions, identity
    )
    try:
        thumb_slides = copy_slides if copy_slides else deck_slides
        for idx, slide in enumerate(thumb_slides, start=1):
            thumb = (
                slides.presentations()
                .pages()
                .getThumbnail(
                    presentationId=thumb_pres_id,
                    pageObjectId=slide["objectId"],
                    thumbnailProperties_thumbnailSize=thumb_size,
                    thumbnailProperties_mimeType="PNG",
                )
                .execute()
            )
            _download(thumb["contentUrl"], bg_dir / f"slide-{idx:03d}.png")
            _p("render", idx, total, f"Rendering slide {idx} of {total}")
            print(f"[import]   slide {idx}: background rendered")
    finally:
        if copy_id:
            try:
                drive.files().delete(fileId=copy_id, supportsAllDrives=True).execute()
            except Exception as e:
                print(f"[import] warning: could not delete temp copy {copy_id}: {e!r}")

    n_img = sum(1 for e in embeds if e["_kind"] == "image")
    n_vid = sum(1 for e in embeds if e["_kind"] == "video")
    for e in embeds:  # _kind is internal bookkeeping only
        e.pop("_kind", None)

    manifest = {
        "title": pres.get("title", "Imported deck"),
        "width": width_px,
        "height": height_px,
        "reveal_theme": theme,
        "images": "slides_png",
        "static_root": ".",
        "embeds": embeds,
    }
    manifest_path = manifest_path or (out / "manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(
        f"[import] wrote {manifest_path}\n"
        f"[import] {total} backgrounds, {n_img} GIF overlay(s), "
        f"{n_vid} video overlay(s); static images left baked in"
    )
    return manifest_path


def _prepare_background_source(slides, drive, pres_id, deck_slides, delete_positions, identity):
    """Make a temp copy of the deck with the overlaid (GIF/video) elements deleted,
    so backgrounds render without a frozen duplicate under each live overlay.

    Returns (presentation_id_to_thumbnail, copy_slides_or_None, copy_id_or_None).
    Falls back to the original deck (copy_id None) if anything goes wrong - the
    result then has the minor doubling, but never crashes or edits your deck.
    """
    if not delete_positions:
        return pres_id, None, None
    copy_id = None
    try:
        copy = drive.files().copy(
            fileId=pres_id,
            body={"name": "[slides-import temp - safe to delete]"},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        copy_id = copy["id"]

        copy_pres = slides.presentations().get(presentationId=copy_id).execute()
        copy_slides = copy_pres.get("slides", [])
        if len(copy_slides) != len(deck_slides):
            raise RuntimeError("copy slide count mismatch")

        delete_ids: list[str] = []
        for idx, (oslide, cslide) in enumerate(zip(deck_slides, copy_slides), start=1):
            positions = delete_positions.get(idx)
            if not positions:
                continue
            om = list(_iter_media(oslide.get("pageElements", []), identity))
            cm = list(_iter_media(cslide.get("pageElements", []), identity))
            if len(om) != len(cm):
                print(f"[import] warning: slide {idx} element count differs in copy; "
                      f"leaving background as-is")
                continue
            for pos in positions:
                oid = cm[pos][1].get("objectId")
                if oid:
                    delete_ids.append(oid)

        if delete_ids:
            slides.presentations().batchUpdate(
                presentationId=copy_id,
                body={"requests": [{"deleteObject": {"objectId": i}} for i in delete_ids]},
            ).execute()
            print(f"[import] rendering clean backgrounds ({len(delete_ids)} live "
                  f"element(s) removed from a temp copy)")
        # Re-fetch so objectIds/order are current after deletion.
        copy_pres = slides.presentations().get(presentationId=copy_id).execute()
        return copy_id, copy_pres.get("slides", []), copy_id
    except Exception as e:
        print(f"[import] warning: clean-background copy failed ({e!r}); "
              f"using original slide render")
        if copy_id:
            try:
                drive.files().delete(fileId=copy_id, supportsAllDrives=True).execute()
            except Exception:
                pass
        return pres_id, None, None


def run(args) -> None:
    try:
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover
        _missing_deps(e)

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    client = args.client or _discover_client(out)

    # Studio mode: one browser flow does sign-in + pick + a live progress bar and
    # then serves the finished deck in the same tab. Delegated to slides_studio.
    if args.studio:
        _run_studio(args, out, client)
        return

    token_path = args.token or (out / "token.json")

    if args.picker:
        import slides_auth_picker as pk

        here = Path(__file__).resolve().parent
        api_key = args.api_key or pk.discover_api_key(out, here, Path.cwd())
        state_path = out / "picker_state.json"
        # Separate token file: the picker uses the narrow drive.file scope and
        # must not reuse a broad-scope token from the other auth flows.
        pk_token = args.token or (out / "token_picker.json")
        override = extract_id(args.url) if args.url else None
        creds, pres_id = pk.authorize(
            client, api_key, pk_token, state_path,
            force_pick=args.pick, override_file_id=override,
        )
    else:
        # Only ask for Drive (metadata) access when we need to search by name.
        need_drive_search = bool(args.name and not args.url)
        creds = get_credentials(args.sa, client, token_path, _scopes(need_drive_search))
        pres_id = extract_id(args.url) if args.url else find_by_name(creds, args.name)

    manifest_path = perform_import(
        creds, pres_id, out, args.theme, args.thumb_size, args.manifest
    )

    if args.build:
        import build_deck

        build_deck.build(manifest_path)


def _run_studio(args, out: Path, client: Path | None) -> None:
    """Wire the picker + progress + deck-serving studio to the import core."""
    import slides_auth_picker as pk
    import slides_studio
    import build_deck as bd

    here = Path(__file__).resolve().parent
    api_key = args.api_key or pk.discover_api_key(out, here, Path.cwd())
    state_path = out / "picker_state.json"
    token_path = args.token or (out / "token_picker.json")
    override = extract_id(args.url) if args.url else None

    def convert(creds, file_id, progress):
        manifest_path = perform_import(
            creds, file_id, out, args.theme, args.thumb_size, progress=progress
        )
        progress("build", 0, 1, "Building interactive deck…")
        # Same-origin relative viewer URLs + vendored reveal served by the studio.
        bd.build(manifest_path, relative_embeds=True, reveal_base="/vendor/reveal", quiet=True)
        progress("done", 1, 1, "Done")

    slides_studio.run_studio(
        client_secret=client,
        api_key=api_key,
        token_path=token_path,
        state_path=state_path,
        out=out,
        vendor_dir=bd.VENDOR,
        convert=convert,
        get_revision=get_revision,
        force_pick=args.pick,
        override_file_id=override,
    )


def get_revision(creds, file_id: str) -> str | None:
    """Return the file's Drive `modifiedTime` (an RFC-3339 timestamp), used to
    tell whether the deck changed since the last import. `drive.file` grants
    metadata access to files the user picked, so no extra scope is needed.
    Returns None if it can't be determined (callers then rebuild to be safe)."""
    try:
        from googleapiclient.discovery import build as gbuild

        drive = gbuild("drive", "v3", credentials=creds, cache_discovery=False)
        meta = (
            drive.files()
            .get(fileId=file_id, fields="modifiedTime", supportsAllDrives=True)
            .execute()
        )
        return meta.get("modifiedTime")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(
        description="Import a Google Slides deck via API, keeping media live"
    )
    ap.add_argument("--url", help="Google Slides URL or presentation ID")
    ap.add_argument("--name", help="Find the deck by (partial) title in your Drive instead of --url")
    ap.add_argument(
        "--picker",
        action="store_true",
        help="No-warning auth: pick the deck in the native Google Picker (drive.file scope)",
    )
    ap.add_argument("--api-key", help="Picker API key (or GOOGLE_API_KEY / api_key.txt)")
    ap.add_argument("--pick", action="store_true", help="Force re-picking the deck (picker mode)")
    ap.add_argument(
        "--studio",
        action="store_true",
        help="One browser flow: sign in + pick + live progress bar, then the deck opens in the same tab (implies --picker)",
    )
    ap.add_argument("--out", type=Path, default=Path("."), help="Output directory (default: .)")
    ap.add_argument("--manifest", type=Path, default=None, help="Manifest path (default: <out>/manifest.json)")
    ap.add_argument("--sa", type=Path, default=None, help="Service account JSON key")
    ap.add_argument("--client", type=Path, default=None, help="OAuth client secret JSON")
    ap.add_argument("--token", type=Path, default=None, help="OAuth token cache (default: <out>/token.json)")
    ap.add_argument("--theme", default="black", help="reveal.js theme (default: black)")
    ap.add_argument(
        "--thumb-size",
        default="LARGE",
        choices=["LARGE", "MEDIUM", "SMALL"],
        help="Background thumbnail size (LARGE=1600px)",
    )
    ap.add_argument("--build", action="store_true", help="Run build_deck.py after import")
    args = ap.parse_args()
    if not args.url and not args.name and not args.picker and not args.studio:
        ap.error("provide --url <slides url/id>, --name <deck title>, --picker, or --studio")
    run(args)


if __name__ == "__main__":
    main()

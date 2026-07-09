"""Shared config resolution for the interactive deck tooling.

build_deck.py (generates the deck) and launch.py (starts the viewer servers +
opens the deck) both import this so the iframe URLs they compute always match.
Port assignment lives here and nowhere else.

Manifest schema (JSON). All paths are relative to the manifest file's directory:

{
  "title": "My Talk",
  "width": 1280,
  "height": 720,
  "images": "slides",                # dir of per-slide PNG/JPG/SVG, OR:
  "pdf": "deck.pdf",                 # a PDF that build_deck splits into images
  "reveal_theme": "black",           # optional reveal.js theme name
  "static_root": "viewers",          # where local viewer HTML lives (default: manifest dir)
  "embeds": [
    {
      "slide": 3,                    # 1-based slide number to attach to
      "serve": "static",            # "static" | "proxy"
      "path": "my_viewer.html",      # file under static_root (static) or backend path (proxy)
      "rect": {"x": 8, "y": 12, "w": 60, "h": 74}   # % of slide; omit = full-bleed
    },
    {
      "slide": 6,
      "serve": "proxy",
      "path": "index.html",
      "backend": {                   # local app to launch + proxy (shim-injected)
        "cmd": ["python", "-m", "http.server", "8000"],
        "port": 8000,
        "cwd": "my_app"             # relative to manifest dir (optional)
      },
      "rect": {"x": 5, "y": 10, "w": 90, "h": 80}
    },
    {
      "slide": 8,
      "serve": "proxy",
      "path": "export/embed.html?bbox=-0.13,51.50,-0.11,51.51&layer=mapnik",
      "backend": { "base": "https://www.openstreetmap.org" }   # external site, proxied
    }
  ]
}
"""
from __future__ import annotations

import json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# Port layout. One shim-injecting static server (rooted at the deck's
# static_root) lives on STATIC_PORT; each proxy viewer gets a port counting up
# from PROXY_PORT_BASE in manifest order.
STATIC_PORT = 9100
PROXY_PORT_BASE = 9101


def load_manifest(manifest_path: Path) -> dict:
    manifest_path = Path(manifest_path).resolve()
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["_manifest_dir"] = str(manifest_path.parent)
    return data


def manifest_dir(manifest: dict) -> Path:
    return Path(manifest["_manifest_dir"])


def get_static_root(manifest: dict) -> Path:
    """Directory the shim-injecting static server serves.

    Defaults to the manifest's own directory; override with "static_root".
    """
    sr = manifest.get("static_root")
    base = manifest_dir(manifest)
    return (base / sr).resolve() if sr else base


def backend_base(embed: dict) -> str | None:
    """Origin a proxy embed forwards to: an external "base" URL or a local
    "port" (paired with an optional "cmd" that launch.py starts)."""
    if embed.get("serve") != "proxy":
        return None
    b = embed.get("backend") or {}
    if b.get("base"):
        return b["base"].rstrip("/")
    if b.get("port"):
        return f"http://127.0.0.1:{b['port']}"
    raise ValueError(f"proxy embed for slide {embed.get('slide')} needs backend.base or backend.port")


def resolve_embeds(manifest: dict) -> list[dict]:
    """Return one resolved dict per embed with runtime ports + iframe URL."""
    resolved: list[dict] = []
    next_proxy_port = PROXY_PORT_BASE

    for emb in manifest.get("embeds", []):
        e = dict(emb)
        mode = e.get("serve", "static")
        path = e.get("path", "").lstrip("/")

        if mode == "static":
            e["serve_port"] = STATIC_PORT
            e["url"] = f"http://127.0.0.1:{STATIC_PORT}/{path}"
        elif mode == "proxy":
            e["serve_port"] = next_proxy_port
            next_proxy_port += 1
            e["url"] = f"http://127.0.0.1:{e['serve_port']}/{path}"
        else:
            raise ValueError(f"embed for slide {e.get('slide')}: unknown serve mode {mode!r}")

        resolved.append(e)

    return resolved


def needs_static_server(resolved: list[dict]) -> bool:
    return any(e.get("serve") == "static" for e in resolved)

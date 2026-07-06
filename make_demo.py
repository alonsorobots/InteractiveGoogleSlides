"""Generate a self-contained demo of the interactive deck tooling.

Creates demo/ with:
  - slides/*.svg          hand-made "slides" (no slide software / PIL needed)
  - cube_viewer.html      a self-contained three.js OrbitControls viewer
  - manifest.json         embeds the cube (static) + live OpenStreetMap (proxy)

Then run:
    python launch.py --manifest demo/manifest.json --build

...and get a deck where Left/Right change slides while mouse-drag orbits the
cube / pans the map. The map slide shows the arrow shim working even for a real
external website (routed through the shim-injecting proxy).

Note: the cube pulls three.js from a CDN and the map loads OSM tiles, so the two
interactive demo slides need internet. Your own local viewers can run offline.
"""
from __future__ import annotations

import json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEMO = PACKAGE_DIR / "demo"
SLIDES = DEMO / "slides"

W, H = 1280, 720
BG = "#0f0f0f"
FG = "#e0e0e0"
ACCENT = "#5b8def"

RECT = {"x": 8, "y": 20, "w": 84, "h": 68}


def _svg(body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}">'
        f'<rect width="{W}" height="{H}" fill="{BG}"/>{body}</svg>'
    )


def _text(x, y, s, size=40, fill=FG, weight="normal", anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-family="Segoe UI, system-ui, sans-serif" '
        f'font-size="{size}" fill="{fill}" font-weight="{weight}" '
        f'text-anchor="{anchor}">{s}</text>'
    )


def _frame_for_embed(label: str) -> str:
    rx, ry = W * RECT["x"] / 100, H * RECT["y"] / 100
    rw, rh = W * RECT["w"] / 100, H * RECT["h"] / 100
    return (
        _text(W / 2, 110, label, size=44, fill=ACCENT, weight="bold", anchor="middle")
        + _text(W / 2, 158, "drag / click = interact \u00b7 \u2190 \u2192 = change slides",
                size=22, fill="#888", anchor="middle")
        + f'<rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" rx="10" '
          f'fill="none" stroke="#333" stroke-width="2" stroke-dasharray="8 6"/>'
    )


def write_slides():
    SLIDES.mkdir(parents=True, exist_ok=True)

    (SLIDES / "slide-001.svg").write_text(_svg(
        _text(W / 2, H / 2 - 40, "Interactive slides", size=76, fill=FG,
              weight="bold", anchor="middle")
        + _text(W / 2, H / 2 + 30, "a live window into HTML, inside a deck",
                size=34, fill=ACCENT, anchor="middle")
        + _text(W / 2, H - 80, "authored like Google Slides \u00b7 presented in reveal.js",
                size=22, fill="#888", anchor="middle")
    ), encoding="utf-8")

    bullets = [
        "Your slides stay as images (Google Slides export).",
        "Chosen slides get a live <iframe> overlay.",
        "A tiny shim forwards \u2190 \u2192 to the deck...",
        "...so every other click stays inside the viewer.",
    ]
    body = _text(90, 130, "How it works", size=52, fill=ACCENT, weight="bold")
    for i, b in enumerate(bullets):
        body += _text(110, 240 + i * 90, "\u2022  " + b, size=34)
    (SLIDES / "slide-002.svg").write_text(_svg(body), encoding="utf-8")

    (SLIDES / "slide-003.svg").write_text(
        _svg(_frame_for_embed("A local 3D viewer (three.js)")), encoding="utf-8")
    (SLIDES / "slide-004.svg").write_text(
        _svg(_frame_for_embed("A live external website (OpenStreetMap)")), encoding="utf-8")


CUBE_VIEWER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Demo cube viewer</title>
<style>
  html, body { margin:0; height:100%; overflow:hidden; background:#111118;
    font-family:'Segoe UI',system-ui,sans-serif; color:#e0e0e0; }
  #tip { position:fixed; left:12px; bottom:12px; background:rgba(0,0,0,.6);
    padding:8px 12px; border-radius:6px; font-size:13px; z-index:10; pointer-events:none; }
  #tip b { color:#5b8def; }
</style>
<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}}
</script>
</head>
<body>
<div id="tip"><b>drag</b> to orbit \u00b7 <b>wheel</b> to zoom \u00b7 <b>\u2190 \u2192</b> change slides</div>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x111118);
const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 0.1, 100);
camera.position.set(3, 2.4, 3.6);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(devicePixelRatio);
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

const cube = new THREE.Mesh(
  new THREE.BoxGeometry(1.6, 1.6, 1.6),
  new THREE.MeshStandardMaterial({ color: 0x5b8def, metalness: 0.2, roughness: 0.4 })
);
scene.add(cube);
scene.add(new THREE.Mesh(
  new THREE.BoxGeometry(1.62,1.62,1.62),
  new THREE.MeshBasicMaterial({ color: 0xffa94d, wireframe: true })
));
scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const dir = new THREE.DirectionalLight(0xffffff, 0.9); dir.position.set(5,8,6); scene.add(dir);
scene.add(new THREE.GridHelper(12, 12, 0x333333, 0x222222));

// If arrows reached this viewer they would spin the cube -- but the injected
// shim intercepts them in the capture phase, so they change slides instead.
addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft')  cube.rotation.y -= 0.3;
  if (e.key === 'ArrowRight') cube.rotation.y += 0.3;
});

addEventListener('resize', () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
(function loop(){ requestAnimationFrame(loop); cube.rotation.y += 0.003; controls.update(); renderer.render(scene, camera); })();
</script>
</body>
</html>
"""


def write_viewer():
    (DEMO / "cube_viewer.html").write_text(CUBE_VIEWER, encoding="utf-8")


def write_manifest():
    osm_bbox = "-0.135,51.503,-0.108,51.517"  # central London
    manifest = {
        "title": "Interactive deck - demo",
        "width": W, "height": H,
        "reveal_theme": "black",
        "images": "slides",
        "static_root": ".",  # this demo dir holds cube_viewer.html
        "embeds": [
            {"slide": 3, "serve": "static", "path": "cube_viewer.html", "rect": RECT},
            {"slide": 4, "serve": "proxy",
             "path": f"export/embed.html?bbox={osm_bbox}&layer=mapnik",
             "backend": {"base": "https://www.openstreetmap.org"},
             "rect": RECT},
        ],
    }
    (DEMO / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main():
    write_slides()
    write_viewer()
    write_manifest()
    print(f"[demo] wrote {DEMO}")
    print("[demo] now run:")
    print("       python launch.py --manifest demo/manifest.json --build")


if __name__ == "__main__":
    main()

"""Shim-injecting server for embedding viewers in the reveal.js deck.

Two modes:

  --mode static --root <dir> --port <p>
      Serve files from <dir>. Any text/html response gets the arrow-key
      forwarding shim injected before </body>.

  --mode proxy --backend http://localhost:8000 --port <p>
      Transparently forward every request to <backend> (same paths, so a
      viewer's root-relative fetch('/api/...') keeps working), injecting the
      shim into HTML responses only. Binary/streaming responses are passed
      through in chunks. Works for external sites too (bypasses X-Frame-Options
      because the browser sees a localhost origin).

The shim is what makes the deck's interaction contract hold: it captures only
ArrowLeft / ArrowRight at the window level (capture phase, so it beats things
like three.js OrbitControls), preventDefaults them, and postMessages the parent
deck to navigate. Every other input (mouse, wheel, other keys) stays in the
viewer. Viewer files are never modified on disk.
"""
from __future__ import annotations

import argparse
import http.server
import socketserver
import urllib.request
import urllib.error
from pathlib import Path

SHIM = b"""
<script>
(function () {
  // Arrow keys always drive the parent deck; everything else stays local.
  window.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      e.stopPropagation();
      try {
        parent.postMessage(
          { type: 'deck-nav', dir: e.key === 'ArrowRight' ? 'right' : 'left' },
          '*'
        );
      } catch (err) {}
    }
  }, true);
})();
</script>
"""


def _inject(body: bytes) -> bytes:
    lower = body.lower()
    idx = lower.rfind(b"</body>")
    if idx == -1:
        return body + SHIM
    return body[:idx] + SHIM + body[idx:]


def _is_html(content_type: str | None) -> bool:
    return bool(content_type) and "text/html" in content_type.lower()


def make_static_handler(root: Path):
    class ShimStaticHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, fmt, *args):
            pass

        def send_head(self):
            path = self.translate_path(self.path)
            p = Path(path)
            if p.is_dir():
                return super().send_head()
            if p.suffix.lower() in (".html", ".htm") and p.exists():
                body = _inject(p.read_bytes())
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._html_body = body
                return None
            return super().send_head()

        def do_GET(self):
            head = self.send_head()
            if head is None and getattr(self, "_html_body", None) is not None:
                self.wfile.write(self._html_body)
                self._html_body = None
            elif head is not None:
                try:
                    self.copyfile(head, self.wfile)
                finally:
                    head.close()

    return ShimStaticHandler


def make_proxy_handler(backend: str):
    backend = backend.rstrip("/")

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        # HTTP/1.0 so responses without a Content-Length (streamed passthrough)
        # frame correctly by closing the connection.
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        def _proxy(self, method: str):
            url = backend + self.path
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = self.rfile.read(length) if length else None

            fwd = urllib.request.Request(url, data=data, method=method)
            has_ua = False
            for k, v in self.headers.items():
                if k.lower() in ("host", "content-length", "connection", "accept-encoding"):
                    continue
                if k.lower() == "user-agent":
                    has_ua = True
                fwd.add_header(k, v)
            if not has_ua:  # some external sites reject a blank UA
                fwd.add_header("User-Agent", "Mozilla/5.0 (deck-proxy)")

            try:
                resp = urllib.request.urlopen(fwd)
            except urllib.error.HTTPError as e:
                resp = e
            except urllib.error.URLError as e:
                self.send_error(502, f"backend unreachable: {e}")
                return

            ctype = resp.headers.get("Content-Type")
            status = getattr(resp, "status", getattr(resp, "code", 200))

            if _is_html(ctype):
                body = _inject(resp.read())
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(body)
                return

            self.send_response(status)
            for k, v in resp.headers.items():
                if k.lower() in ("transfer-encoding", "connection", "content-encoding"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            if method != "HEAD":
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break

        def do_GET(self):
            self._proxy("GET")

        def do_HEAD(self):
            self._proxy("HEAD")

        def do_POST(self):
            self._proxy("POST")

    return ProxyHandler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="Shim-injecting static/proxy server")
    ap.add_argument("--mode", choices=["static", "proxy"], required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--root", default=None, help="static mode: directory to serve (default: cwd)")
    ap.add_argument("--backend", default=None, help="proxy mode: http://host:port to forward to")
    args = ap.parse_args()

    if args.mode == "static":
        root = Path(args.root).resolve() if args.root else Path.cwd()
        handler = make_static_handler(root)
        print(f"[serve] static  :{args.port}  root={root}")
    else:
        if not args.backend:
            ap.error("proxy mode requires --backend")
        handler = make_proxy_handler(args.backend)
        print(f"[serve] proxy   :{args.port}  -> {args.backend}")

    _Server(("", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()

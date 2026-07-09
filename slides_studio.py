"""Studio server: one browser flow from Google sign-in to a live, playing deck.

This turns the import into a single in-browser experience:

  sign in with Google  ->  pick the deck (native Picker)  ->  live progress bar
  ->  the finished interactive deck loads *in the same tab*.

Everything is served from this one local origin, fully offline (the vendored
reveal.js is served from /vendor/reveal), so:
  - /                       redirects to the picker / progress / deck as needed
  - /oauth2callback         completes the drive.file OAuth (loopback)
  - /picker                 the native Google Picker
  - /picked                 records the chosen file, kicks off conversion
  - /progress , /status     the live progress bar + its JSON feed
  - /deck.html              the finished reveal.js deck (no shim)
  - /_media/*.html          per-element viewers, with the arrow-key shim injected
  - /slides_png/* , /media/* slide backgrounds and extracted media
  - /vendor/reveal/*        vendored reveal.js (offline)

The heavy lifting (fetch/render/extract/build) is supplied by the caller as a
`convert(creds, file_id, progress)` callback so this module stays purely about
auth + serving + progress.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import serve  # reuse the arrow-key shim + injector
import slides_auth_picker as pk


_PROGRESS_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Converting your deck…</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#0d0d0f;color:#eee;
       display:flex;align-items:center;justify-content:center;height:100vh}
  .card{width:min(90vw,34rem);text-align:center}
  h1{font-weight:600;font-size:1.3rem;margin:0 0 1.5rem}
  .bar{height:14px;border-radius:7px;background:#26262b;overflow:hidden}
  .fill{height:100%;width:0;border-radius:7px;background:linear-gradient(90deg,#4f8cff,#7dd3fc);
        transition:width .35s ease}
  .msg{margin-top:1rem;color:#aaa;min-height:1.4em}
  .err{color:#ff6b6b;white-space:pre-wrap;text-align:left;margin-top:1rem}
</style></head>
<body>
  <div class="card">
    <h1>Converting your presentation…</h1>
    <div class="bar"><div class="fill" id="fill"></div></div>
    <div class="msg" id="msg">Starting…</div>
    <div class="err" id="err"></div>
  </div>
<script>
  function poll() {
    fetch('/status').then(function (r) { return r.json(); }).then(function (s) {
      if (s.error) {
        document.getElementById('err').textContent = 'Error: ' + s.error;
        return;
      }
      var pct = (s.percent != null)
        ? s.percent
        : (s.total ? (s.current / s.total) * 100 : (s.done ? 100 : 5));
      // Belt-and-suspenders: the bar must never move backwards on the client.
      window.__pct = Math.max(window.__pct || 0, pct);
      document.getElementById('fill').style.width = Math.round(window.__pct) + '%';
      document.getElementById('msg').textContent = s.message || '';
      if (s.done) {
        document.getElementById('fill').style.width = '100%';
        document.getElementById('msg').textContent = 'Opening your deck…';
        setTimeout(function () { window.location.href = '/deck.html'; }, 500);
        return;
      }
      setTimeout(poll, 400);
    }).catch(function () { setTimeout(poll, 800); });
  }
  poll();
</script>
</body></html>
"""


# Injected into the served deck so a browser refresh re-imports the latest slides
# only when the deck actually changed on Google's side. F5 / Ctrl-R do a normal
# browser reload (no keydown interception - the reveal.js re-init is fast enough
# that the "flash" isn't worth breaking users' expectations of the refresh key).
# On any load that was itself a reload, we ask /check (a cheap Drive modifiedTime
# lookup) and only navigate to the progress bar when it reports a change; a
# rebuild lands back on /deck.html via a normal navigation, so it never loops.
_DECK_REFRESH_JS = """
<script>
(function () {
  function gate() {
    fetch('/check').then(function (r) { return r.json(); }).then(function (j) {
      if (j.changed) { window.location.href = '/progress?refresh=1'; }
    }).catch(function () { window.location.href = '/progress?refresh=1'; });
  }
  try {
    var e = performance.getEntriesByType('navigation')[0];
    var t = e ? e.type : (performance.navigation && performance.navigation.type === 1
                            ? 'reload' : 'navigate');
    if (t === 'reload') { gate(); }
  } catch (err) {}
})();
</script>
"""


def _safe_join(root: Path, rel: str) -> Path | None:
    p = (root / rel.lstrip("/")).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def _load_revision(path: Path, file_id: str) -> str | None:
    """Return the remembered Drive modifiedTime for this file, if any."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("file_id") == file_id:
            return data.get("revision")
    except (OSError, ValueError):
        pass
    return None


def _save_revision(path: Path, file_id: str, revision: str | None) -> None:
    try:
        path.write_text(
            json.dumps({"file_id": file_id, "revision": revision}), encoding="utf-8"
        )
    except OSError:
        pass


def run_studio(
    client_secret: Path | None,
    api_key: str | None,
    token_path: Path,
    state_path: Path,
    out: Path,
    vendor_dir: Path,
    convert,
    get_revision=None,
    force_pick: bool = False,
    override_file_id: str | None = None,
) -> None:
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as e:  # pragma: no cover
        pk._missing_deps(e)

    if not api_key:
        raise SystemExit(
            "Studio mode needs a Google API key (GOOGLE_API_KEY or api_key.txt) for the Picker."
        )

    creds = pk._load_cached(token_path)
    file_id = override_file_id or pk._load_file_id(state_path)
    app_id = pk._app_id_from_client(client_secret) if client_secret else ""
    revision_path = out / "studio_state.json"

    class StudioServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = StudioServer(("localhost", 0), None)
    port = srv.server_address[1]

    ctx = {
        "creds": creds,
        "file_id": file_id,
        "flow": None,
        "status": {"stage": "idle", "current": 0, "total": 0, "percent": 0, "message": "", "done": False, "error": None},
        "started": False,
        "running": False,
        "revision": _load_revision(revision_path, file_id) if file_id else None,
    }
    convert_lock = threading.Lock()

    if not creds:
        if not client_secret:
            raise SystemExit("Studio mode needs an OAuth client secret (--client) on first run.")
        flow = Flow.from_client_secrets_file(
            str(client_secret),
            scopes=[pk.DRIVE_FILE],
            redirect_uri=f"http://localhost:{port}/oauth2callback",
        )
        ctx["flow"] = flow

    def start_convert(refresh: bool = False):
        with convert_lock:
            if ctx["running"]:
                return  # a conversion is in flight - ignore reloads fired mid-run
            if ctx["started"] and not refresh:
                return  # already converted once; a plain revisit shouldn't rerun
            ctx["running"] = True
            ctx["started"] = True
            ctx["status"] = {"stage": "idle", "current": 0, "total": 0, "percent": 0,
                             "message": "", "done": False, "error": None}
        pk._save_file_id(state_path, ctx["file_id"])

        def worker():
            # Each phase owns a slice of the single 0-100 bar so overall progress
            # only ever moves forward (analyze fills 5->55, render 55->99), instead
            # of resetting to 0 when the second pass begins.
            bands = {"fetch": (0, 5), "analyze": (5, 55), "render": (55, 99)}

            def progress(stage, cur, total, msg):
                lo, hi = bands.get(stage, (0, 99))
                frac = (cur / total) if total else 0.0
                pct = lo + (hi - lo) * max(0.0, min(1.0, frac))
                pct = max(ctx["status"].get("percent", 0) or 0, pct)  # never regress
                ctx["status"].update(stage=stage, current=cur, total=total,
                                     message=msg, percent=pct)
            try:
                # If we already have a built deck and know its revision, skip the
                # rebuild when Google's copy hasn't changed (covers restarts and
                # direct /progress hits; the /check gate handles the no-flash case).
                if get_revision and ctx["revision"] and (out / "deck.html").is_file():
                    ctx["status"].update(stage="check", message="Checking for changes…")
                    current = get_revision(ctx["creds"], ctx["file_id"])
                    if current and current == ctx["revision"]:
                        ctx["status"].update(done=True, stage="unchanged", percent=100,
                                             message="No changes - up to date")
                        return
                convert(ctx["creds"], ctx["file_id"], progress)
                # Remember what we just imported so a later refresh can tell
                # whether Google's copy changed and skip a needless rebuild.
                if get_revision:
                    rev = get_revision(ctx["creds"], ctx["file_id"])
                    ctx["revision"] = rev
                    _save_revision(revision_path, ctx["file_id"], rev)
                ctx["status"].update(done=True, stage="done", percent=100, message="Done")
            except SystemExit as e:
                ctx["status"].update(error=str(e))
            except Exception as e:  # surface to the browser rather than dying silently
                ctx["status"].update(error=repr(e))
            finally:
                ctx["running"] = False

        threading.Thread(target=worker, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype="text/html; charset=utf-8", status=200):
            # Browsers routinely abort in-flight requests (navigating away, reload,
            # closing the tab). That surfaces as ConnectionAborted/BrokenPipe here;
            # it's harmless, so swallow it instead of dumping a traceback.
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (ConnectionError, BrokenPipeError):
                pass

        def _serve_file(self, path: Path, shim: bool = False):
            if not path or not path.is_file():
                self._send(b"Not found", status=404)
                return
            data = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if shim and path.suffix.lower() in (".html", ".htm"):
                data = serve._inject(data)
                ctype = "text/html; charset=utf-8"
            self._send(data, ctype=ctype)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            path = u.path

            # --- auth + flow endpoints ---
            if path == "/oauth2callback":
                code = qs.get("code", [None])[0]
                if not code:
                    self._send(b"Missing authorization code.", status=400)
                    return
                ctx["flow"].fetch_token(code=code)
                ctx["creds"] = ctx["flow"].credentials
                token_path.write_text(ctx["creds"].to_json(), encoding="utf-8")
                self._send(pk._picker_html(ctx["creds"].token, api_key, app_id, redirect="/progress"))
                return

            if path == "/picker":
                self._send(pk._picker_html(ctx["creds"].token, api_key, app_id, redirect="/progress"))
                return

            if path == "/picked":
                if qs.get("cancel"):
                    ctx["status"].update(error="No deck was selected.")
                else:
                    ctx["file_id"] = qs.get("id", [None])[0]
                    start_convert()
                self._send(b'{"ok":true}', ctype="application/json")
                return

            if path == "/progress":
                # If we already have creds + file id (permanence path), convert now.
                # ?refresh=1 (a browser refresh of the deck) re-imports the latest.
                if ctx["creds"] and ctx["file_id"]:
                    start_convert(refresh=bool(qs.get("refresh")))
                self._send(_PROGRESS_PAGE.encode("utf-8"))
                return

            if path == "/status":
                self._send(json.dumps(ctx["status"]).encode("utf-8"), ctype="application/json")
                return

            if path == "/check":
                # Has the deck changed on Google since our last import? Compare the
                # Drive modifiedTime; on any uncertainty, report changed so we rebuild.
                changed = True
                if get_revision and ctx["creds"] and ctx["file_id"] and ctx["revision"]:
                    try:
                        current = get_revision(ctx["creds"], ctx["file_id"])
                        changed = not (current and current == ctx["revision"])
                    except Exception:
                        changed = True
                self._send(
                    json.dumps({"changed": changed, "revision": ctx["revision"]}).encode("utf-8"),
                    ctype="application/json",
                )
                return

            # --- served deck assets (only meaningful once conversion is done) ---
            if path.startswith("/vendor/reveal/"):
                rel = path[len("/vendor/reveal/"):]
                self._serve_file(_safe_join(vendor_dir, rel))
                return

            if path.startswith("/_media/"):
                self._serve_file(_safe_join(out, path), shim=True)
                return

            if path in ("/", "/deck.html"):
                deck = out / "deck.html"
                if not deck.is_file():
                    # Not built yet - send them into the conversion flow instead.
                    self._send(_PROGRESS_PAGE.encode("utf-8"))
                    return
                data = deck.read_bytes()
                data = data.replace(
                    b"</body>", _DECK_REFRESH_JS.encode("utf-8") + b"</body>", 1
                )
                self._send(data)
                return

            # backgrounds, media, and anything else under the output dir
            target = _safe_join(out, path)
            self._serve_file(target)

    srv.RequestHandlerClass = Handler

    # Decide the entry URL for the browser.
    if ctx["creds"] and ctx["file_id"] and not force_pick:
        entry = f"http://localhost:{port}/progress"        # straight to conversion
    elif ctx["creds"]:
        entry = f"http://localhost:{port}/picker"          # signed in, needs a pick
    else:
        auth_url, _ = ctx["flow"].authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="false"
        )
        entry = auth_url                                    # first-time sign-in

    print(f"[studio] open: http://localhost:{port}/  (Ctrl-C to stop)")
    webbrowser.open(entry)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[studio] stopped.")
    finally:
        srv.server_close()

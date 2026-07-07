"""No-scary-warning auth for Slides import: drive.file scope + Google Picker.

The default OAuth flow requests *sensitive* scopes (presentations.readonly /
drive.readonly), which triggers Google's "this app isn't verified" warning and
the alarming "see all your Google Drive" wording. This module avoids all of that
by using only the **non-sensitive `drive.file` scope**: the app can touch *only*
the file the user explicitly picks in the native Google Picker. Non-sensitive
scopes don't show the unverified-app screen, need no Google verification, and
have no 100-user cap.

Flow (first run):
  1. Loopback OAuth on localhost with scope=drive.file, access_type=offline, so
     we get a durable refresh token. The consent screen is the benign per-file
     one - no warning.
  2. The OAuth success page *is* the Google Picker. The user clicks their deck.
     Picking a file with the app's token + appId grants the app drive.file
     access to that one file (this association persists across token refreshes).
  3. The picked file id is posted back to the loopback server.

Later runs: the cached refresh token + remembered file id mean no browser at all
- fully permanent, still no warning. Pass force_pick=True to choose a new deck.

Setup (developer, once): enable the Google Picker API, create an API key, and
use a Desktop-app OAuth client (loopback redirect is allowed automatically).
"""
from __future__ import annotations

import http.server
import json
import os
import urllib.parse
import webbrowser
from pathlib import Path

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"


def _missing_deps(exc: Exception):
    raise SystemExit(
        "The Google auth libraries are required for the Picker flow.\n"
        "    pip install google-api-python-client google-auth google-auth-oauthlib\n"
        f"(import error: {exc})"
    )


# --------------------------------------------------------------------------- #
# Discovery of the two developer-provided values
# --------------------------------------------------------------------------- #
def discover_api_key(*dirs: Path) -> str | None:
    """Find the Picker API key: env GOOGLE_API_KEY / GOOGLE_PICKER_API_KEY, or a
    plain-text api_key.txt / picker_api_key.txt in any of the given dirs."""
    for var in ("GOOGLE_API_KEY", "GOOGLE_PICKER_API_KEY"):
        v = os.environ.get(var)
        if v:
            return v.strip()
    for base in dirs:
        for name in ("api_key.txt", "picker_api_key.txt"):
            p = base / name
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
    return None


def _app_id_from_client(client_secret_path: Path) -> str:
    """The Picker's setAppId wants the project number, which is the first segment
    of the OAuth client_id (before the first '-')."""
    d = json.loads(Path(client_secret_path).read_text(encoding="utf-8"))
    node = d.get("installed") or d.get("web") or {}
    return node.get("client_id", "").split("-")[0]


# --------------------------------------------------------------------------- #
# Cached, refreshable credentials (the permanence path)
# --------------------------------------------------------------------------- #
def _load_cached(token_path: Path):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as e:  # pragma: no cover
        _missing_deps(e)
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path), [DRIVE_FILE])
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    return None


def _load_file_id(state_path: Path) -> str | None:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8")).get("file_id")
        except Exception:
            return None
    return None


def _save_file_id(state_path: Path, file_id: str) -> None:
    state_path.write_text(json.dumps({"file_id": file_id}), encoding="utf-8")


# --------------------------------------------------------------------------- #
# The served pages
# --------------------------------------------------------------------------- #
_PICKER_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pick your deck</title>
<style>
  body{{font-family:system-ui,sans-serif;margin:0;padding:2rem;background:#111;color:#eee}}
  .msg{{max-width:36rem;margin:4rem auto;text-align:center;line-height:1.5}}
</style></head>
<body>
<div class="msg" id="msg">Opening the Google Picker&hellip; choose the presentation to import.</div>
<script>
  var TOKEN = {token};
  var API_KEY = {api_key};
  var APP_ID = {app_id};
  function onApiLoad() {{ gapi.load('picker', createPicker); }}
  function createPicker() {{
    var view = new google.picker.DocsView(google.picker.ViewId.PRESENTATIONS)
      .setMode(google.picker.DocsViewMode.LIST)
      .setOwnedByMe(true);
    var shared = new google.picker.DocsView(google.picker.ViewId.PRESENTATIONS)
      .setEnableDrives(true);
    var picker = new google.picker.PickerBuilder()
      .setAppId(APP_ID)
      .setOAuthToken(TOKEN)
      .setDeveloperKey(API_KEY)
      .addView(view)
      .addView(shared)
      .setTitle('Pick the deck to import')
      .setCallback(pickerCallback)
      .build();
    picker.setVisible(true);
  }}
  function pickerCallback(data) {{
    var a = google.picker.Action;
    if (data.action === a.PICKED) {{
      var id = data.docs[0].id;
      fetch('/picked?id=' + encodeURIComponent(id)).then(function () {{
        {on_picked}
      }});
    }} else if (data.action === a.CANCEL) {{
      fetch('/picked?cancel=1');
      document.getElementById('msg').innerHTML =
        '<h2>Cancelled.</h2><p>You can close this tab.</p>';
    }}
  }}
</script>
<script src="https://apis.google.com/js/api.js?onload=onApiLoad"></script>
</body></html>
"""

_DONE_PAGE = b"<html><body style='font-family:sans-serif'><h2>Done. You can close this tab.</h2></body></html>"


def _picker_html(token: str, api_key: str, app_id: str, redirect: str | None = None) -> bytes:
    if redirect:
        on_picked = f"window.location.href = {json.dumps(redirect)};"
    else:
        on_picked = (
            "document.getElementById('msg').innerHTML = "
            "'<h2>Got it.</h2><p>You can close this tab and return to the terminal.</p>';"
        )
    return _PICKER_PAGE.format(
        token=json.dumps(token),
        api_key=json.dumps(api_key),
        app_id=json.dumps(app_id),
        on_picked=on_picked,
    ).encode("utf-8")


# --------------------------------------------------------------------------- #
# Loopback server orchestration
# --------------------------------------------------------------------------- #
def _make_server():
    class _Srv(http.server.HTTPServer):
        allow_reuse_address = True

    # port 0 -> OS picks a free loopback port
    return _Srv(("localhost", 0), None)


def authorize(
    client_secret: Path | None,
    api_key: str | None,
    token_path: Path,
    state_path: Path,
    force_pick: bool = False,
    override_file_id: str | None = None,
):
    """Return (credentials, file_id) using the drive.file + Picker flow.

    Reuses a cached refresh token (and remembered file id) when possible, so most
    runs need no browser at all.
    """
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as e:  # pragma: no cover
        _missing_deps(e)

    creds = _load_cached(token_path)
    file_id = override_file_id or _load_file_id(state_path)

    # Fast path: everything cached, nothing to pick.
    if creds and file_id and not force_pick:
        return creds, file_id

    if not api_key:
        raise SystemExit(
            "The Picker flow needs a Google API key. Set GOOGLE_API_KEY, or put it "
            "in api_key.txt next to the manifest. (Create one in Cloud Console -> "
            "APIs & Services -> Credentials -> API key, and enable the Picker API.)"
        )
    if not creds and not client_secret:
        raise SystemExit("The Picker flow needs an OAuth client secret (--client).")

    server = _make_server()
    port = server.server_address[1]
    result: dict = {"creds": creds, "file_id": None, "cancelled": False}

    flow = None
    if not creds:
        flow = Flow.from_client_secrets_file(
            str(client_secret),
            scopes=[DRIVE_FILE],
            redirect_uri=f"http://localhost:{port}/oauth2callback",
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="false"
        )

    app_id = _app_id_from_client(client_secret) if client_secret else ""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype="text/html; charset=utf-8", status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path == "/oauth2callback":
                code = qs.get("code", [None])[0]
                if not code:
                    self._send(b"Missing authorization code.", status=400)
                    return
                flow.fetch_token(code=code)
                result["creds"] = flow.credentials
                token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
                self._send(_picker_html(flow.credentials.token, api_key, app_id))
                return

            if parsed.path == "/picker":
                tok = result["creds"].token
                self._send(_picker_html(tok, api_key, app_id))
                return

            if parsed.path == "/picked":
                if qs.get("cancel"):
                    result["cancelled"] = True
                else:
                    result["file_id"] = qs.get("id", [None])[0]
                self._send(_DONE_PAGE)
                return

            self._send(b"", status=404)

    server.RequestHandlerClass = Handler

    print("[picker] opening browser for Google sign-in + deck selection ...")
    webbrowser.open(auth_url if flow else f"http://localhost:{port}/picker")

    try:
        while result["file_id"] is None and not result["cancelled"]:
            server.handle_request()
    finally:
        server.server_close()

    if result["cancelled"] or not result["file_id"]:
        raise SystemExit("[picker] no deck was selected.")

    _save_file_id(state_path, result["file_id"])
    return result["creds"], result["file_id"]

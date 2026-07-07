# Live-media import (`slides_import.py`)

Pull a deck straight from the **Google Slides API** and keep its **GIFs and
videos live** in the presented deck — instead of baking them flat.

## Why this exists (the intent)

The rest of this project treats a slide as a flat picture: you export to
PDF/PNG (or auto-sync the PDF), and every slide becomes a background image with
hand-placed interactive `<iframe>` overlays on top. That's perfect for *design*
fidelity, but it **destroys embedded motion**: a rasterizer turns an animated
GIF into a single frame and a video into its poster still. The media is gone
before any of our code sees it.

`slides_import.py` takes the *privileged* path. With API access to the deck you
don't get a picture — you get the deck's structure. Every element is described
with its type and its exact affine transform, so we can:

1. **Keep the flat design** — each slide is still rendered to a background PNG
   (server-side, via the Slides `getThumbnail` endpoint at up to 1600px), so
   text, shapes, and layout look identical to Slides.
2. **Re-attach the live media** — for every image/GIF and every video element we
   compute its on-slide rectangle and drop a **real, live overlay** there: an
   `<img>` for GIFs, a YouTube/Drive `<iframe>` for videos. GIFs animate and
   videos play, right inside the deck.

The result is a normal `manifest.json` + asset folders that the existing
`build_deck.py` / `launch.py` consume **unchanged**. This is a second on-ramp,
parallel to the PDF/images flow — it doesn't replace or alter it. Overlays reuse
the exact same arrow-key shim and interaction contract as every other embed, so
**Left/Right still change slides; everything else is interactivity.**

### What maps to what

| In Google Slides | Becomes |
|---|---|
| Slide (whole) | Background PNG in `slides_png/` |
| Image / animated GIF | `<img>` overlay (`serve: static`) + bytes in `media/` |
| YouTube video | `youtube.com/embed/<id>` iframe overlay (`serve: static`) |
| Drive video | `drive.google.com/file/d/<id>/preview` iframe overlay |
| Element position/size | `rect` (percent box), from the element's affine transform |

Videos in Slides are almost always references (YouTube/Drive), not embedded
blobs — which is exactly why they become simple iframe overlays with no download.

## Recommended: the no-warning Picker flow (`--picker`, `drive.file`)

If you want the friendliest experience — a normal "Sign in with Google," **no
scary "this app isn't verified" screen, no "see all your Drive" wording, no
Google verification, no user cap** — use `--picker`.

It works by requesting only the **non-sensitive `drive.file` scope**. Google only
shows the unverified-app warning for *sensitive/restricted* scopes, so with
`drive.file` there is no warning at all. You then pick your deck in the **native
Google Picker**, which grants the app access to *only that one file*. A durable
refresh token is saved, and the picked file is remembered, so later runs need no
browser at all.

Developer setup (once):

1. Cloud project → **enable the Google Slides API, Google Drive API, and Google
   Picker API**.
2. **Credentials → OAuth client ID → Desktop app** → download as
   `client_secret.json` (loopback redirect is allowed automatically).
3. **Credentials → API key** → save it as `api_key.txt` next to the manifest (or
   set `GOOGLE_API_KEY`). Restrict the key to the Picker API.

Run:

```bash
python slides_import.py --picker --out mydeck --build
# first run: browser opens -> sign in -> pick the deck. No warning screen.
# later runs: no browser; pass --pick to choose a different deck.
```

Notes:
- `--name` search is **not** available with `--picker` (searching all of Drive
  needs a sensitive scope). The Picker *is* the deck-finder.
- The API key appears in the local Picker page (as with all web Picker apps);
  restrict it to the Picker API. It is git-ignored by default.

## One-tab studio: `--studio` (sign in → progress bar → deck, in the same tab)

`--studio` turns the whole thing into a single browser flow. You sign in and
pick the deck (same no-warning `drive.file` Picker as above), then **the same tab
shows a live progress bar as each slide converts, and when it finishes the
finished interactive deck loads right there** — no terminal round-trip, no
separate `build_deck`/`launch` step.

```bash
python slides_import.py --studio --out mydeck --client client_secret.json
# first run: sign in -> pick -> watch it convert -> the deck plays in the tab
# later runs: no picking; it re-imports the remembered deck and reopens it
# pass --pick to choose a different deck
```

`--studio` implies `--picker` (it needs the API key + client secret) and does
**not** need `--build` — building happens automatically. It leaves the tab open
serving the deck; press `Ctrl-C` in the terminal to stop the server.

Everything is served from that one local origin and is **fully offline**: the
vendored `reveal.js` is served from `/vendor/reveal`, backgrounds and extracted
media from the output dir, and the per-element viewers (`/_media/*.html`) get the
same arrow-key interaction shim as the normal viewer. The only network use is the
one-time Google fetch of your slides during conversion.

**Refresh to re-import - but only if it changed.** While viewing the deck, a
browser **refresh (F5 / Ctrl-R / the reload button)** checks whether the deck
changed on Google and only rebuilds if it did:

- On refresh the page first asks `/check`, which compares the file's Drive
  `modifiedTime` against the one saved from the last import.
- **Unchanged** → nothing happens; you stay on the deck you're already viewing
  (no rebuild, no flash).
- **Changed** → you get the progress bar again, then the updated deck.

The last-imported `modifiedTime` is saved to `studio_state.json` (keyed by file
id), so this also survives restarting the server: reopening an unchanged deck
skips the import and goes straight to the deck. Detection uses a keypress
intercept (instant, flash-free) plus the Navigation Timing API as a catch-all,
and a reload fired mid-conversion is ignored so mashing F5 can't stack up
importers. If the revision can't be read for any reason, it rebuilds to be safe.

## Alternative auth models (auto-detected)

These use broader scopes (which show the one-time unverified warning until you
verify the app) but need no API key or Picker. The script auto-selects based on
what you provide.

### A. Service account (recommended for a dedicated present/sync box)
A robot identity with a static JSON key. **No interactive login, ever.**

1. [Google Cloud Console](https://console.cloud.google.com/) → new project.
2. **APIs & Services → Library** → enable **Google Slides API** and **Google
   Drive API**.
3. **IAM & Admin → Service Accounts** → create one → **Keys → Add key → JSON**.
   Save it as `service_account.json` (already git-ignored).
4. **Share your deck** (or a folder of decks) with the service account's email
   (`...@...iam.gserviceaccount.com`), just like sharing with a person.

```bash
python slides_import.py --url "<slides url>" --out mydeck --sa service_account.json --build
# or export GOOGLE_APPLICATION_CREDENTIALS=service_account.json and drop --sa
```

### B. OAuth installed app (rides on your own Google account)
One browser login; a cached refresh token then refreshes silently.

1. Same project + enable both APIs.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Desktop app.** Download it as `client_secret.json` (git-ignored).
3. **OAuth consent screen → Publish app** (set to *In production*). This is the
   one gotcha that bites everyone: while the app is in **Testing**, Google
   expires refresh tokens after **7 days**, forcing weekly re-logins. Publishing
   makes them effectively permanent — and you do **not** need to pass Google's
   verification review for single-user use; an app can be *In production* and
   *unverified* at the same time.

```bash
python slides_import.py --url "<slides url>" --out mydeck --client client_secret.json --build
# first run opens a browser once; subsequent runs reuse mydeck/token.json
```

## Options

```
--url         Google Slides URL or presentation ID   (--url, --name, or --picker)
--name        Find the deck by (partial) title in your Drive (needs auth)
--picker      No-warning auth: pick the deck in the native Google Picker (drive.file)
--studio      One tab: sign in + pick + progress bar, then the deck opens in place (implies --picker)
--api-key     Picker API key (or GOOGLE_API_KEY / api_key.txt)
--pick        Force re-picking the deck (picker/studio mode)
--out         Output directory                        (default: .)
--sa          Service account JSON key                (or GOOGLE_APPLICATION_CREDENTIALS)
--client      OAuth client secret JSON
--token       OAuth token cache                       (default: <out>/token.json)
--manifest    Manifest path                           (default: <out>/manifest.json)
--theme       reveal.js theme                         (default: black)
--thumb-size  LARGE | MEDIUM | SMALL background size  (default: LARGE = 1600px)
--build       Run build_deck.py after importing
```

Find a deck by name instead of pasting a URL (this is why we ask for Drive
access — it lets the tool search your Slides):

```bash
python slides_import.py --name "Q3 Roadmap" --out mydeck --client client_secret.json --build
```

Then present as usual:

```bash
python launch.py --manifest mydeck/manifest.json
```

### A note on "HTML" media

Google Slides can only hold **images/GIFs** and **video references** (YouTube /
Drive) — it cannot embed live HTML apps. So this importer handles GIFs and
videos automatically. For a live *HTML* app on a slide (a three.js scene, a web
app, an external site), that's the manifest's hand-authored `embeds` with
`serve: static` / `serve: proxy` (see the main README) — add those entries to
the generated `manifest.json` alongside the auto-extracted media.

## Notes and limitations

- The live overlay sits **on top of** the flat poster in the background PNG
  (same rectangle), so a paused video/first GIF frame shows through only until
  the overlay renders. `getThumbnail` PNGs have no alpha, so we cover rather than
  cut a hole — visually seamless for full-opacity media.
- **Rotation** is handled via the transform's vector lengths; **shear** is
  ignored (the Slides API itself flags sizing as approximate under shear).
- Cross-origin video iframes (YouTube) capture their own keys; click back onto
  the deck if arrow-key navigation stops responding while a video is focused.
- `getThumbnail` counts as an *expensive* read for API quota; large decks make
  one such call per slide.
- Media `contentUrl`s are temporary but are downloaded immediately at import
  time, so the built deck is self-contained.

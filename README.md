# SoundCloud Backuper

Downloads every album, EP, compilation and standalone single from a SoundCloud
artist profile, preserving the original audio format whenever the uploader has
made the track downloadable. Tracks are renamed to match the title shown on
SoundCloud and each release gets its own folder with its cover art.

```
backup/<artist>/
    albums/<Album Title>/cover.jpg + NN - Track.<ext>
    eps/<EP Title>/cover.jpg + NN - Track.<ext>
    compilations/<Comp Title>/cover.jpg + NN - Track.<ext>
    singles/<Track Title>.<ext>  +  <Track Title>.jpg
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and fill in `SOUNDCLOUD_OAUTH_TOKEN` (see below).

## Authentication — important

SoundCloud no longer permits programmatic email + password login (hCaptcha
blocks every third-party tool, including `yt-dlp`). You must reuse the
browser session of an account that's already logged in. Two ways:

### Option A — let yt-dlp read your browser cookies (easiest)

1. Log in to <https://soundcloud.com> in Firefox (or Chrome / Brave / Edge…).
2. **Fully close that browser** — yt-dlp can't read its cookie DB while it's
   running and holding a lock on the file.
3. In `.env` set:
   ```
   COOKIES_FROM_BROWSER=firefox
   ```
   (other accepted values: `chrome`, `chromium`, `brave`, `edge`, `opera`,
   `vivaldi`, `safari`).

### Option B — paste the `oauth_token` manually

Useful if you don't want to close your browser.

- **Firefox:** `F12` → **Storage** tab → **Cookies** → `https://soundcloud.com`
  → find the row `oauth_token`, double-click its **Value** column, copy.
- **Chrome / Edge / Brave:** `F12` → **Application** tab → **Cookies** →
  `https://soundcloud.com` → copy the `oauth_token` value.

The value looks like `2-123456-7890123-AbCdEfGhIjKlMn`. Paste it into `.env`:

```
SOUNDCLOUD_OAUTH_TOKEN=2-123456-...
```

The token is long-lived (months). If downloads start failing with 401, just
grab a fresh one the same way.

## Run

Three modes are available:

```bash
# (default) Try to grab the uploader's ORIGINAL file (WAV/FLAC/AIFF/MP3) via
# the public API. Only works for tracks where the uploader ticked
# "enable downloads". Slow (heavily rate-limited).
python backup.py

# Streams-only: skip the originals endpoint entirely. Grabs every track at
# streaming quality. Much faster.
#   - 256 kbps AAC if the account is Go+
#   - 128 kbps AAC/MP3 otherwise
python backup.py --streams-only

# Originals-api: use the SoundCloud OWNER-ONLY endpoint to fetch the original
# master file (FLAC/WAV/AIFF/MP3) for EVERY track on the account — even
# tracks where the uploader didn't tick "enable downloads".
# REQUIRES that you own the account (logged-in user == track uploader).
python backup.py --originals-api
```

**Recommended workflow for an account you own:**

```bash
python backup.py --originals-api        # grab original masters for everything
python backup.py --streams-only         # (optional) fill in any gaps
```

Re-running is safe — existing files are skipped (matched by base filename
regardless of extension).

## Notes

- Original WAV / FLAC / AIFF is only available in default mode and only for
  tracks where the uploader ticked “enable downloads”. Use `--streams-only`
  to grab everything else at streaming quality.
- Playlists are intentionally skipped — only releases that appear on the
  artist’s `/albums` page are treated as collections.
- Folder classification (`albums` / `eps` / `compilations`) follows the
  release’s `set_type` set by the uploader on SoundCloud.

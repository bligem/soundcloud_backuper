"""
SoundCloud account backup tool.

Downloads every album / EP / compilation and every standalone single from a
SoundCloud artist profile, organised like:

    backup/<artist>/
        albums/<Album Title>/
            cover.jpg
            01 - Track One.<ext>
            02 - Track Two.<ext>
        eps/<EP Title>/
            ...
        compilations/<Compilation Title>/
            ...
        singles/
            Track Title.<ext>
            Track Title.jpg

Original audio file format is preserved when the uploader has enabled the
"downloadable" flag (often WAV / FLAC / AIFF). Otherwise the highest available
stream is saved (256kbps AAC if you have Go+ via the oauth token, else
128kbps MP3).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

load_dotenv()

ARTIST_URL: str = os.getenv(
    "SOUNDCLOUD_ARTIST_URL", "https://soundcloud.com/enilylbmessa"
).rstrip("/")
OAUTH_TOKEN: str | None = os.getenv("SOUNDCLOUD_OAUTH_TOKEN") or None
COOKIES_FROM_BROWSER: str | None = (
    os.getenv("COOKIES_FROM_BROWSER") or None
)
OUTPUT_DIR: Path = Path(os.getenv("OUTPUT_DIR", "backup"))

# Set by main() from CLI flags
STREAMS_ONLY: bool = False
OWNER_API: bool = False

# Cache the resolved oauth token so we don't re-extract from Firefox per track
_resolved_token: str | None = None
_owner_api_last_call: float = 0.0


def get_owner_token() -> str | None:
    """Return the oauth_token to use for owner-only API calls.

    Either the explicit SOUNDCLOUD_OAUTH_TOKEN from .env, or — if cookies are
    being read from the browser — extract `oauth_token` from there.
    """
    global _resolved_token
    if _resolved_token is not None:
        return _resolved_token or None
    if OAUTH_TOKEN:
        _resolved_token = OAUTH_TOKEN
        return _resolved_token
    if COOKIES_FROM_BROWSER:
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(COOKIES_FROM_BROWSER.lower())
            for cookie in jar:
                if cookie.name == "oauth_token" and "soundcloud.com" in (cookie.domain or ""):
                    _resolved_token = cookie.value
                    return _resolved_token
        except Exception as e:
            print(f"  ! could not extract oauth_token from {COOKIES_FROM_BROWSER}: {e}",
                  file=sys.stderr)
    _resolved_token = ""
    return None


def _owner_api_throttle(min_gap: float = 1.5) -> None:
    """Enforce a minimum gap between calls to the owner /download endpoint —
    SoundCloud rate-limits it aggressively (one rapid second-call = 403)."""
    import time as _time
    global _owner_api_last_call
    gap = _time.monotonic() - _owner_api_last_call
    if gap < min_gap:
        _time.sleep(min_gap - gap)
    _owner_api_last_call = _time.monotonic()


# Map content-type returned by the CloudFront URL to a filename extension.
_CT_TO_EXT = {
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".m4a",
    "audio/ogg": ".ogg",
}


def download_owner_master(track_id: int, out_dir: Path, basename: str) -> bool:
    """Download the *original* master file for a track using the owner-only
    `/tracks/<id>/download` endpoint. Requires that the logged-in user owns
    the track. Returns True on success."""
    token = get_owner_token()
    if not token:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = already_downloaded(out_dir, basename)
    if existing:
        print(f"    ⤳ skip (exists): {existing.name}")
        return True

    h = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
    for attempt in range(1, 7):
        _owner_api_throttle()
        r = requests.get(
            f"https://api-v2.soundcloud.com/tracks/{track_id}/download",
            headers=h,
            timeout=30,
        )
        if r.status_code == 200:
            break
        if r.status_code in (403, 429):
            wait = min(60, 2 ** attempt)
            print(f"    … owner-API {r.status_code}, backing off {wait}s (try {attempt}/6)")
            import time as _t; _t.sleep(wait)
            continue
        print(f"    ! owner-API HTTP {r.status_code}: {r.text[:120]}", file=sys.stderr)
        return False
    else:
        print("    ! owner-API gave up after retries", file=sys.stderr)
        return False

    try:
        master_url = r.json()["redirectUri"]
    except (ValueError, KeyError) as e:
        print(f"    ! owner-API bad response: {e}", file=sys.stderr)
        return False

    with requests.get(master_url, stream=True, timeout=60) as fr:
        if fr.status_code != 200:
            print(f"    ! master HTTP {fr.status_code}", file=sys.stderr)
            return False
        ct = (fr.headers.get("content-type") or "").split(";")[0].strip().lower()
        ext = _CT_TO_EXT.get(ct)
        if not ext:
            # Try to glean from content-disposition: filename*=utf-8''Foo.flac
            cd = fr.headers.get("content-disposition", "")
            m = re.search(r"filename\*?=(?:utf-8'')?\"?([^\";]+)", cd)
            if m and "." in m.group(1):
                ext = "." + m.group(1).rsplit(".", 1)[-1].lower()
            else:
                ext = ".bin"
        out_path = out_dir / (basename + ext)
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        size = 0
        with open(tmp_path, "wb") as f:
            for chunk in fr.iter_content(1024 * 256):
                f.write(chunk)
                size += len(chunk)
        tmp_path.rename(out_path)
        print(f"    ✓ master ({ext[1:]}, {size/1024/1024:.2f} MiB) → {out_path.name}")
        return True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    """Make a string safe to use as a file / directory name on Linux & Windows."""
    name = _INVALID_FS_CHARS.sub("_", name).strip().strip(".").strip()
    # collapse repeated whitespace
    name = re.sub(r"\s+", " ", name)
    return name or "untitled"


def hi_res_artwork(url: str | None) -> str | None:
    """Upgrade a SoundCloud artwork URL to the largest available variant."""
    if not url:
        return None
    return re.sub(
        r"-(large|t\d+x\d+|small|badge|tiny|crop|original)\.(jpg|png|jpeg)$",
        r"-original.\2",
        url,
    )


def ydl_opts_base() -> dict:
    if STREAMS_ONLY:
        # Streaming-only mode: skip the rate-limited "original download" endpoint
        # entirely. Grabs every track regardless of the uploader's "enable
        # downloads" setting and is much faster (no per-track 429 throttling).
        # Quality: 256k AAC if your account has Go+ via the cookie/token,
        # otherwise 128k AAC / MP3.
        fmt = "hls_aac_256k/hls_aac_160k/hls_aac_96k/http_mp3_128k/hls_mp3_128k/bestaudio/best"
        sleep_requests = 0.4
        sleep_dl = 0
        concurrent_frags = 4
    else:
        # Default ("originals") mode: try the uploader-provided original download
        # first (WAV / FLAC / AIFF / MP3 — whichever was uploaded), then fall back
        # to best stream. SoundCloud aggressively rate-limits the original
        # endpoint with HTTP 429, so we throttle outgoing requests heavily.
        fmt = "download/http_mp3_320k/hls_aac_256k/hls_aac_160k/hls_mp3_128k/bestaudio/best"
        sleep_requests = 1.5
        sleep_dl = 2
        concurrent_frags = 2

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 10,
        "sleep_interval_requests": sleep_requests,
        "sleep_interval": sleep_dl,
        "max_sleep_interval": 8,
        "retry_sleep_functions": {
            "http": lambda n: min(60, 2 ** n),
            "fragment": lambda n: min(30, 2 ** n),
            "extractor": lambda n: min(60, 2 ** n),
        },
        "concurrent_fragment_downloads": concurrent_frags,
        "format": fmt,
        # do NOT recode; keep original container (wav/flac/m4a/mp3/etc.)
        "postprocessors": [],
        "writethumbnail": False,
    }
    if COOKIES_FROM_BROWSER:
        # yt-dlp will read the cookie database from your browser profile.
        # Browser must be FULLY CLOSED (it locks its cookies DB while running).
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER.lower(),)
    elif OAUTH_TOKEN:
        # yt-dlp's SoundCloud extractor accepts the oauth_token via username="oauth"
        opts["username"] = "oauth"
        opts["password"] = OAUTH_TOKEN
    return opts


def extract(url: str, flat: bool = False) -> dict | None:
    opts = ydl_opts_base()
    opts["extract_flat"] = "in_playlist" if flat else False
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"  ! extract failed for {url}: {e}", file=sys.stderr)
            return None


def download_artwork(url: str | None, out_path: Path) -> None:
    url = hi_res_artwork(url)
    if not url:
        return
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and r.content:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(r.content)
            print(f"    ✓ artwork → {out_path.name}")
        else:
            # fall back to the originally provided (non -original) URL
            r = requests.get(url.replace("-original.", "-t500x500."), timeout=30)
            if r.status_code == 200 and r.content:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(r.content)
                print(f"    ✓ artwork → {out_path.name}")
    except Exception as e:
        print(f"    ! artwork failed: {e}", file=sys.stderr)


def already_downloaded(out_dir: Path, basename: str) -> Path | None:
    if not out_dir.exists():
        return None
    for p in out_dir.iterdir():
        if p.is_file() and p.stem == basename and p.suffix.lower() not in (
            ".part",
            ".ytdl",
            ".json",
            ".jpg",
            ".jpeg",
            ".png",
        ):
            return p
    return None


def download_track(track_url: str, out_dir: Path, basename: str,
                   track_id: int | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = already_downloaded(out_dir, basename)
    if existing:
        print(f"    ⤳ skip (exists): {existing.name}")
        return

    # Owner-API mode: try to grab the original master via the owner-only
    # /tracks/<id>/download endpoint. Falls back to yt-dlp if that fails
    # (e.g. you don't own this particular track or the master is gone).
    if OWNER_API and track_id is not None:
        if download_owner_master(track_id, out_dir, basename):
            return
        print("    … owner-API failed, falling back to yt-dlp streams")

    opts = ydl_opts_base()
    opts["outtmpl"] = str(out_dir / (basename + ".%(ext)s"))
    opts["skip_download"] = False
    opts["quiet"] = False
    opts["noprogress"] = False
    with YoutubeDL(opts) as ydl:
        ydl.download([track_url])


# ---------------------------------------------------------------------------
# classification of SoundCloud "sets"
# ---------------------------------------------------------------------------

# SoundCloud "set_type" values seen in the wild:
#   album, ep, single, compilation, playlist
# yt-dlp surfaces this as `playlist_type` (preferred) and sometimes `set_type`.
SET_TYPE_TO_DIR = {
    "album": "albums",
    "ep": "eps",
    "compilation": "compilations",
    "single": "singles",
    "playlist": "albums",  # shouldn't appear under /albums, but fall through
}


def classify(info: dict) -> str:
    raw = (
        info.get("playlist_type")
        or info.get("set_type")
        or info.get("_type")
        or ""
    ).lower()
    return SET_TYPE_TO_DIR.get(raw, "albums")


# ---------------------------------------------------------------------------
# main processing
# ---------------------------------------------------------------------------


def process_album(album_url: str, artist_dir: Path) -> set[int]:
    """Download a single album / EP / compilation. Returns the set of track ids
    contained in it (so we can later skip them when downloading singles)."""
    info = extract(album_url, flat=False)
    if not info:
        return set()

    title = info.get("title") or "Untitled"
    kind = classify(info)
    folder = artist_dir / kind / sanitize(title)
    folder.mkdir(parents=True, exist_ok=True)

    entries = [e for e in (info.get("entries") or []) if e]
    print(f"\n📀 [{kind}] {title}  ({len(entries)} tracks)")

    # SoundCloud never populates `thumbnail` on the playlist/album object itself —
    # only on individual track entries. Grab from the album info first (just in
    # case), then fall back to the highest-quality thumbnail from the first entry.
    def best_thumbnail(obj: dict) -> str | None:
        thumbs = obj.get("thumbnails") or []
        if thumbs:
            # prefer the 'original' variant, else pick the last (largest) one
            orig = next((t["url"] for t in thumbs if t.get("id") == "original"), None)
            return orig or thumbs[-1].get("url")
        return obj.get("thumbnail")

    artwork = best_thumbnail(info)
    if not artwork and entries:
        artwork = best_thumbnail(entries[0])
    download_artwork(artwork, folder / "cover.jpg")

    track_ids: set[int] = set()
    for idx, entry in enumerate(entries, start=1):
        ttitle = entry.get("title") or f"track_{idx}"
        tid = entry.get("id")
        if tid is not None:
            try:
                track_ids.add(int(tid))
            except (TypeError, ValueError):
                pass
        basename = f"{idx:02d} - {sanitize(ttitle)}"
        turl = entry.get("webpage_url") or entry.get("url")
        if not turl:
            print(f"    ! no URL for track: {ttitle}")
            continue
        try:
            download_track(turl, folder, basename, track_id=tid)
        except Exception as e:
            print(f"    ! track failed ({ttitle}): {e}", file=sys.stderr)
    return track_ids


def process_singles(artist_dir: Path, exclude_ids: set[int]) -> None:
    print("\n=== Singles (tracks not part of any album/EP/compilation) ===")
    listing = extract(ARTIST_URL + "/tracks", flat=True)
    entries: Iterable[dict] = (listing or {}).get("entries") or []
    entries = [e for e in entries if e]

    singles_dir = artist_dir / "singles"
    count = 0
    for entry in entries:
        tid = entry.get("id")
        try:
            tid_int = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid_int = None
        if tid_int is not None and tid_int in exclude_ids:
            continue

        ttitle = entry.get("title") or "untitled"
        turl = entry.get("url") or entry.get("webpage_url")
        if not turl:
            continue
        tid = entry.get("id")
        basename = sanitize(ttitle)
        count += 1
        print(f"\n🎵 {ttitle}")

        # fetch full info so we have the artwork URL
        full = extract(turl, flat=False) or {}
        artwork = full.get("thumbnail")
        if not artwork:
            thumbs = full.get("thumbnails") or []
            if thumbs:
                artwork = thumbs[-1].get("url")
        if artwork:
            singles_dir.mkdir(parents=True, exist_ok=True)
            download_artwork(artwork, singles_dir / (basename + ".jpg"))

        try:
            download_track(turl, singles_dir, basename, track_id=tid)
        except Exception as e:
            print(f"    ! single failed ({ttitle}): {e}", file=sys.stderr)

    print(f"\nProcessed {count} singles.")


def main() -> int:
    global STREAMS_ONLY, OWNER_API
    parser = argparse.ArgumentParser(
        description="Back up a SoundCloud artist's full catalogue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Modes:
  (default)          Try to grab the uploader's ORIGINAL download (WAV/FLAC/
                     AIFF/MP3) when available, falling back to the best stream.
                     Slower because SoundCloud rate-limits the originals API
                     and the script throttles itself to compensate.

  --streams-only     Skip the originals endpoint entirely. Grabs every track
                     regardless of whether the uploader ticked "enable
                     downloads". Much faster. Quality is 256k AAC (with Go+
                     token) or 128k AAC/MP3. Use this for tracks that the
                     default mode couldn't download.

  --originals-api    Use the SoundCloud owner-only /tracks/<id>/download
                     endpoint to fetch the ORIGINAL master file (FLAC/WAV/
                     AIFF/MP3) for EVERY track on the account — even tracks
                     where the uploader didn't tick "enable downloads".
                     ONLY works for tracks you OWN (the logged-in account
                     must be the uploader). Slow (heavily rate-limited).

Re-running is safe: existing files are skipped (matched by base filename
regardless of extension), so you can do an --originals-api pass first and
then a --streams-only pass to fill any gaps.
""",
    )
    parser.add_argument(
        "--streams-only",
        action="store_true",
        help="Skip the original-download endpoint; grab streams for everything.",
    )
    parser.add_argument(
        "--originals-api",
        action="store_true",
        help="Use the owner-only SoundCloud API to fetch master files (FLAC/WAV) "
             "for every track. Requires ownership of the account.",
    )
    args = parser.parse_args()
    if args.streams_only and args.originals_api:
        parser.error("--streams-only and --originals-api are mutually exclusive")
    STREAMS_ONLY = args.streams_only
    OWNER_API = args.originals_api

    artist_handle = ARTIST_URL.rstrip("/").split("/")[-1]
    artist_dir = OUTPUT_DIR / sanitize(artist_handle)
    artist_dir.mkdir(parents=True, exist_ok=True)

    if OWNER_API:
        mode_desc = "OWNER-API (original masters via owner-only endpoint — account must own all tracks)"
    elif STREAMS_ONLY:
        mode_desc = "STREAMS-ONLY (fast, AAC/MP3)"
    else:
        mode_desc = "originals-preferred (slow, may fail on non-downloadable tracks)"

    print(f"SoundCloud backup")
    print(f"  artist : {ARTIST_URL}")
    print(f"  output : {artist_dir.resolve()}")
    print(f"  mode   : {mode_desc}")
    if COOKIES_FROM_BROWSER:
        auth_desc = f"cookies from {COOKIES_FROM_BROWSER}"
    elif OAUTH_TOKEN:
        auth_desc = "oauth token loaded"
    else:
        auth_desc = "ANONYMOUS (no auth in .env)"
    print(f"  auth   : {auth_desc}")
    if not (COOKIES_FROM_BROWSER or OAUTH_TOKEN):
        print("    ⚠  Without auth you will only get tracks that SoundCloud serves")
        print("       publicly. Older / paywalled originals may fail.")

    # 1) Albums / EPs / compilations
    print("\n=== Enumerating /albums (covers albums + EPs + compilations) ===")
    listing = extract(ARTIST_URL + "/albums", flat=True)
    album_entries = [e for e in ((listing or {}).get("entries") or []) if e]
    print(f"Found {len(album_entries)} release(s).")

    seen_track_ids: set[int] = set()
    for entry in album_entries:
        aurl = entry.get("url") or entry.get("webpage_url")
        if not aurl:
            continue
        try:
            ids = process_album(aurl, artist_dir)
            seen_track_ids |= ids
        except Exception as e:
            print(f"  ! album failed: {e}", file=sys.stderr)

    # 2) Standalone singles
    try:
        process_singles(artist_dir, seen_track_ids)
    except Exception as e:
        print(f"  ! singles step failed: {e}", file=sys.stderr)

    print("\n✅ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

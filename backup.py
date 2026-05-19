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
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 10,
        # SoundCloud aggressively rate-limits the per-track "original download"
        # endpoint with HTTP 429. Throttling outgoing requests + retrying with
        # backoff is the only way to reliably grab originals across 100+ tracks.
        "sleep_interval_requests": 1.5,
        "sleep_interval": 2,
        "max_sleep_interval": 8,
        "retry_sleep_functions": {
            "http": lambda n: min(60, 2 ** n),
            "fragment": lambda n: min(30, 2 ** n),
            "extractor": lambda n: min(60, 2 ** n),
        },
        "concurrent_fragment_downloads": 2,
        # Prefer the uploader-provided original download (WAV/FLAC/AIFF/MP3
        # depending on what they uploaded). If unavailable, fall back to the
        # best stream (256k AAC with Go+ token, else 128k MP3).
        "format": "download/http_mp3_320k/hls_aac_256k/hls_aac_160k/hls_mp3_128k/bestaudio/best",
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


def download_track(track_url: str, out_dir: Path, basename: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = already_downloaded(out_dir, basename)
    if existing:
        print(f"    ⤳ skip (exists): {existing.name}")
        return
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
            download_track(turl, folder, basename)
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
            download_track(turl, singles_dir, basename)
        except Exception as e:
            print(f"    ! single failed ({ttitle}): {e}", file=sys.stderr)

    print(f"\nProcessed {count} singles.")


def main() -> int:
    artist_handle = ARTIST_URL.rstrip("/").split("/")[-1]
    artist_dir = OUTPUT_DIR / sanitize(artist_handle)
    artist_dir.mkdir(parents=True, exist_ok=True)

    print(f"SoundCloud backup")
    print(f"  artist : {ARTIST_URL}")
    print(f"  output : {artist_dir.resolve()}")
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

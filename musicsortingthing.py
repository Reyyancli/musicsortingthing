#!/usr/bin/env python3
"""Watch a music root folder and organize tracks by album or hierarchy.

Features:
- Scans a user-chosen root folder repeatedly (or once with --once flag).
- Detects newly added audio files, zip archives, and album art images.
- Reads album metadata from each track using Mutagen.
- Default Sorting: Root / Album / original_filename.mp3
- Hierarchy Sorting (--hierarchy): Root / Main Artist / Album / [Disc] / 01 Title.mp3
- Smart Orphan Logic: Moves guest/collab tracks to an 'Orphan' folder under the Main Artist.
- Smart Prefix Detection: Cleans existing track/disc prefixes from filenames and titles.
- Automatic Cover Art: Moves external images to album folders and instantly embeds them.
- Automatic Cleanup: Recursively checks and safely deletes empty leftover folders (30s cooldown).
- Converts non-MP3 files to MP3 while preserving metadata and album art seamlessly.

Requirements:
- mutagen
- ffmpeg on PATH

Example:
    python music_album_organizer.py ~/music --once --hierarchy
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
import difflib
import json
import unicodedata
from collections import deque
from typing import Dict, Iterable, Optional, Tuple

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import Picture
    from mutagen.id3 import APIC, ID3
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "This script requires 'mutagen'. Install it with: pip install mutagen"
    ) from exc


# ---------------------------------------------------------------------------
# Terminal colours (ANSI 16-colour palette; disabled when not a TTY)
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    import sys
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Enable VT processing on Windows 10+
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
    return True

_COLOR = _supports_color()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

def clr_gray(t: str) -> str:    return _c("90", t)
def clr_yellow(t: str) -> str:  return _c("33", t)
def clr_red(t: str) -> str:     return _c("31", t)
def clr_green(t: str) -> str:   return _c("32", t)
def clr_bold(t: str) -> str:    return _c("1",  t)
def clr_cyan(t: str) -> str:    return _c("36", t)


AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".alac",
    ".wma",
}

ARCHIVE_EXTENSIONS = {".zip"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
WATCH_EXTENSIONS = AUDIO_EXTENSIONS | ARCHIVE_EXTENSIONS | IMAGE_EXTENSIONS

DEFAULT_INTERVAL_SECONDS = 3.0
DEFAULT_SETTLE_SECONDS = 1.5
CLEANUP_INTERVAL_SECONDS = 30.0
MP3_SUFFIX = ".mp3"
ZIPS_DIR_NAME = "Zips"
IGNORE_LIST_FILE = ".organizer_ignorelist.json"
ALBUM_IGNORE_FILE = ".organizer_album_ignore.json"
DEFAULT_CLEANUP_COOLDOWN_SECONDS = 120.0

# Global Caches for Performance and Context Awareness
ALBUM_MAIN_ARTIST_CACHE: Dict[tuple[str, str], str] = {}
ALBUM_MULTI_DISC_CACHE: Dict[tuple, bool] = {}  # keyed by (artist_sanitized, album_sanitized)
METADATA_CACHE: Dict[Path, Tuple[int, dict]] = {}

# Session caches for interactive prompts
MISSING_ALBUMARTIST_ASKED: set = set()
ALBUMARTIST_ALL_APPROVED: Dict[str, str] = {}
ARTIST_PARTIAL_MATCH_ASKED: set = set()
ALBUM_PARTIAL_MATCH_ASKED: set = set()
ALBUM_MERGE_DECISIONS: Dict[tuple, str] = {}
ALBUM_IGNORE_ALL: set = set()  # (artist_sanitized, album_sanitized) — skip all partial matches
RECENT_ALBUM_DIRS: deque = deque(maxlen=20)  # album folders recently written to, newest first

# Safe-mode conflict collectors — populated when --safe is active and a check is disabled
SAFE_ARTIST_CONFLICTS: set = set()   # artist name pairs that would have been prompted
SAFE_ALBUM_CONFLICTS: set = set()    # (artist, album) pairs that would have been prompted

# Set once at startup from command-line args
_DRY_RUN: bool = False
_REPROCESS_ORPHANS: bool = False

_METADATA_CACHE_MAX = 5_000   # evict oldest entry beyond this
_MAX_SETTLE_SECONDS = 60.0    # process a file even if still changing after this long


class SafeAbortError(Exception):
    """Raised in --safe mode when a partial-match check is disabled and a conflict is detected."""


@dataclass(frozen=True)
class FileStamp:
    size: int
    mtime_ns: int


@dataclass
class PendingEntry:
    first_seen: float
    first_ever_seen: float
    stamp: FileStamp
    asked_conversion: bool = False


def load_ignore_list(root: Path) -> set:
    path = root / IGNORE_LIST_FILE
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
    except Exception as exc:
        logging.warning("Could not load ignore list: %s", exc)
    return set()


def save_ignore_list(root: Path, ignore_list: set) -> None:
    if _DRY_RUN:
        logging.info("DRY-RUN: would save folder ignore list (%d entries)", len(ignore_list))
        return
    path = root / IGNORE_LIST_FILE
    tmp  = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ignore_list), f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logging.warning("Could not save ignore list: %s", exc)
        try: tmp.unlink(missing_ok=True)
        except OSError: pass


def load_album_ignore_list(root: Path) -> set:
    """Load the persisted 'ignore all partial matches' set for albums."""
    path = root / ALBUM_IGNORE_FILE
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return {tuple(item.split("|", 1)) for item in data if "|" in item}
    except Exception as exc:
        logging.warning("Could not load album ignore list: %s", exc)
    return set()


def save_album_ignore_list(root: Path, album_ignore: set) -> None:
    if _DRY_RUN:
        logging.info("DRY-RUN: would save album ignore list (%d entries)", len(album_ignore))
        return
    path = root / ALBUM_IGNORE_FILE
    tmp  = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(f"{a}|{b}" for a, b in album_ignore), f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logging.warning("Could not save album ignore list: %s", exc)
        try: tmp.unlink(missing_ok=True)
        except OSError: pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch a music directory and organize tracks by album."
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Root music folder to watch, for example '~/music' or '/mnt/music'",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check and organize all files in the directory once, then exit immediately.",
    )
    parser.add_argument(
        "--hierarchy",
        action="store_true",
        help="Sort as: Root / Main Artist / Album / [Disc] / 01 Title",
    )
    parser.add_argument(
        "--skip-art",
        action="store_true",
        help="Disable automatic moving and embedding of album art images (.jpg, .png).",
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Disable deletion of empty folders and clutter cleanup prompts.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between scans (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help=(
            "How long a file must remain unchanged before processing "
            f"(default: {DEFAULT_SETTLE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without moving, converting, extracting, or deleting anything",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("debug", "info", "warning", "error"),
        help="Logging verbosity (default: info)",
    )
    parser.add_argument(
        "--manage-ignorelist",
        action="store_true",
        help="Interactively view and remove entries from the deletion ignore list, then exit.",
    )
    parser.add_argument(
        "--manage-album-ignorelist",
        action="store_true",
        help="Interactively view, remove, or add entries to the album partial-match ignore list, then exit.",
    )
    parser.add_argument(
        "--cleanup-cooldown",
        type=float,
        default=DEFAULT_CLEANUP_COOLDOWN_SECONDS,
        help=(
            "Seconds a non-media folder must remain unchanged before a deletion prompt is shown. "
            f"Prevents prompts while new files are still being added (default: {DEFAULT_CLEANUP_COOLDOWN_SECONDS})"
        ),
    )
    parser.add_argument(
        "--no-convert",
        action="store_true",
        help="Skip MP3 conversion entirely; non-MP3 audio files are left in place.",
    )
    parser.add_argument(
        "--no-partial-album",
        action="store_true",
        help="Disable partial album name matching (never prompt to merge similar album names).",
    )
    parser.add_argument(
        "--no-partial-artist",
        action="store_true",
        help="Disable partial album-artist matching (never prompt to merge similar artist names).",
    )
    parser.add_argument(
        "--no-folder-ignorelist",
        action="store_true",
        help="Ignore the persisted folder-deletion ignore list for this run.",
    )
    parser.add_argument(
        "--no-album-ignorelist",
        action="store_true",
        help="Ignore the persisted album partial-match ignore list for this run.",
    )
    parser.add_argument(
        "--safe",
        action="store_true",
        help=(
            "When combined with --no-partial-album or --no-partial-artist, abort moving any track "
            "that would have triggered a prompt instead of silently skipping it. "
            "With --once, prints a summary of all held-back tracks at the end."
        ),
    )
    parser.add_argument(
        "--reprocess-orphans",
        action="store_true",
        help=(
            "Include files inside 'Orphan' folders when scanning. "
            "Useful after manually correcting albumartist tags on stranded tracks."
        ),
    )
    return parser.parse_args()


class _ColorFormatter(logging.Formatter):
    _LEVEL_COLORS = {
        logging.DEBUG:    "90",   # gray
        logging.INFO:     "90",   # gray
        logging.WARNING:  "33",   # yellow
        logging.ERROR:    "31",   # red
        logging.CRITICAL: "31",   # red
    }

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if not _COLOR:
            return msg
        # Color only the levelname token, leave the timestamp plain
        color = self._LEVEL_COLORS.get(record.levelno, "0")
        colored_level = f"\033[{color}m{record.levelname}\033[0m"
        # The formatted string contains the literal levelname; replace first occurrence
        return msg.replace(record.levelname, colored_level, 1)


def configure_logging(level_name: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        _ColorFormatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.root.setLevel(getattr(logging, level_name.upper()))
    logging.root.handlers = [handler]


def normalize_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Root directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {resolved}")
    return resolved


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def is_zip_file(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_EXTENSIONS


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


_builtin_input = input   # capture builtin before any local shadowing

def _input(prompt: str = "") -> str:
    """Wraps input(); returns '' on EOF (piped stdin, closed terminal)."""
    try:
        return _builtin_input(prompt)
    except EOFError:
        return ""


_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
})


def has_collision_suffix(filename: str) -> bool:
    """Check if filename has a collision suffix like ' (1)', ' (2)', etc. (1–99 only)."""
    return bool(re.search(r'\s+\([1-9]\d?\)\.[^.]+$', filename))


def iter_watch_files(root: Path) -> Iterable[Path]:
    zips_root = archive_storage_dir(root).resolve()
    for path in root.rglob("*"):
        try:
            if "Orphan" in path.parts and not _REPROCESS_ORPHANS:
                continue
                
            if path.is_file() and path.suffix.lower() in WATCH_EXTENSIONS:
                if path.suffix.lower() in ARCHIVE_EXTENSIONS:
                    try:
                        if zips_root in path.resolve().parents:
                            continue
                    except Exception:
                        pass
                # Skip files with collision suffixes - they're already organized
                if has_collision_suffix(path.name):
                    continue
                yield path
        except OSError:
            continue


def build_snapshot(root: Path) -> Dict[Path, FileStamp]:
    snapshot: Dict[Path, FileStamp] = {}
    for path in iter_watch_files(root):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        snapshot[path] = FileStamp(
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
        )
    return snapshot


def scan_for_changes(
    previous: Dict[Path, FileStamp], current: Dict[Path, FileStamp]
) -> tuple[list[Path], list[Path], list[Path]]:
    added = [path for path in current if path not in previous]
    removed = [path for path in previous if path not in current]
    modified = [
        path for path in current if path in previous and previous[path] != current[path]
    ]
    return added, removed, modified


def sanitize_folder_name(name: str) -> str:
    cleaned = name.strip()
    cleaned = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.upper() in _WINDOWS_RESERVED:
        return "Unknown Album"
    return cleaned


def sanitize_file_name(name: str) -> str:
    cleaned = name.strip()
    cleaned = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Unknown"


def within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def extract_track_and_title(raw_title: str) -> tuple[Optional[str], str]:
    pattern = re.compile(
        r'^'                           
        r'[\[\(]?'                     
        r'(?:(\d{1,2})[-_])?'          
        r'(\d{1,4})'                   
        r'[\]\)]?'                     
        r'[\s\.\-_]*'                  
        r'(?:-[\s]+)?'                 
        r'(.*)'                        
        , re.IGNORECASE
    )
    
    match = pattern.match(raw_title)
    if not match:
        return None, raw_title
    
    disc_part = match.group(1)
    track_part = match.group(2)
    rest_of_title = match.group(3)
    
    if not disc_part and len(track_part) >= 3:
        track_num = track_part[-2:]
    else:
        track_num = track_part
        
    cleaned_title = re.sub(r'^[\s\.\-_]+', '', rest_of_title).strip()
    
    if not cleaned_title:
        cleaned_title = f"Track {int(track_num)}"
        
    return track_num, cleaned_title


def parse_metadata(path: Path) -> dict:
    data = {
        "album": None,
        "artist": None,
        "albumartist": None,
        "title": None,
        "track_num": None,
        "track_raw": None,
        "disc_num": None,
        "disc_total": None,
        "disc_raw": None,
    }

    def get_tag(tags, keys):
        for k in keys:
            if k in tags:
                val = tags[k]
                return str(val[0]).strip() if isinstance(val, list) and val else str(val).strip()
        return None

    try:
        audio = MutagenFile(path, easy=True)
        if not audio or not getattr(audio, "tags", None):
            return data
            
        tags = audio.tags
        data["album"] = get_tag(tags, ['album', 'ALBUM', 'TALB'])
        data["albumartist"] = get_tag(tags, ['albumartist', 'ALBUMARTIST', 'TPE2'])
        data["artist"] = data["albumartist"] or get_tag(tags, ['artist', 'ARTIST', 'TPE1'])
        data["title"] = get_tag(tags, ['title', 'TITLE', 'TIT2'])
        
        t_raw = get_tag(tags, ['tracknumber', 'TRACKNUMBER', 'TRCK'])
        if t_raw:
            data["track_raw"] = t_raw
            data["track_num"] = t_raw.split('/')[0].strip()

        d_raw = get_tag(tags, ['discnumber', 'DISCNUMBER', 'TPOS'])
        if d_raw:
            data["disc_raw"] = d_raw
            parts = d_raw.split('/')
            data["disc_num"] = parts[0].strip()
            if len(parts) > 1:
                data["disc_total"] = parts[1].strip()

    except Exception as exc:
        logging.debug("Failed reading metadata from %s: %s", path, exc)

    return data


def get_cached_metadata(path: Path) -> dict:
    """Reads metadata heavily utilizing an in-memory cache mapped to file modification timestamps."""
    try:
        stat_res = path.stat()
        stamp = stat_res.st_mtime_ns
    except OSError:
        stamp = 0
        
    if path in METADATA_CACHE:
        cached_stamp, cached_meta = METADATA_CACHE[path]
        if cached_stamp == stamp:
            return cached_meta
            
    meta = parse_metadata(path)
    if stamp > 0:
        if len(METADATA_CACHE) >= _METADATA_CACHE_MAX:
            METADATA_CACHE.pop(next(iter(METADATA_CACHE)))  # evict oldest (insertion order)
        METADATA_CACHE[path] = (stamp, meta)
    return meta


def extract_tag_value(meta: MutagenFile, keys: list[str]) -> str:
    if not meta:
        return ""
    if hasattr(meta, 'tags') and meta.tags:
        for key in keys:
            if key in meta.tags:
                val = meta.tags[key]
                if isinstance(val, list) and val:
                    return str(val[0])
                elif hasattr(val, "text") and val.text:
                    return str(val.text[0])
                return str(val)
    for key in keys:
        if hasattr(meta, key):
            val = getattr(meta, key)
            if isinstance(val, list) and val:
                return str(val[0])
            return str(val)
    return ""


def score_track_group(tracks: list[Path], artist_name: str) -> dict:
    track_count = len(tracks)
    album_artist_matches = 0
    album_artist_populated_count = 0
    total_tags_count = 0
    embedded_art_count = 0
    total_size = 0
    
    aa_keys = ["albumartist", "TPE2", "aART", "album artist"]
    
    for track in tracks:
        try:
            if not track.exists():
                continue
            stat_res = track.stat()
            total_size += stat_res.st_size
            
            meta = MutagenFile(track)
            if meta:
                total_tags_count += len(meta.keys())
                if hasattr(meta, 'tags') and meta.tags:
                    if any(k in meta.tags for k in ["APIC:", "covr"]):
                        embedded_art_count += 1
                if hasattr(meta, 'pictures') and meta.pictures:
                    embedded_art_count += 1
                    
                album_artist = extract_tag_value(meta, aa_keys)
                if album_artist:
                    album_artist_populated_count += 1
                    if album_artist.lower() == artist_name.lower():
                        album_artist_matches += 1
        except Exception:
            continue
            
    richness_score = total_tags_count + (embedded_art_count * 10)
    
    return {
        "artist_name": artist_name,
        "track_count": track_count,
        "album_artist_matches": album_artist_matches,
        "album_artist_populated_count": album_artist_populated_count,
        "total_size": total_size,
        "richness_score": richness_score
    }


def evaluate_album_priority(score_a: dict, score_b: dict) -> int:
    if score_a["track_count"] != score_b["track_count"]:
        return 1 if score_a["track_count"] > score_b["track_count"] else -1
        
    a_has_matching_aa = score_a["album_artist_matches"] > 0
    b_has_matching_aa = score_b["album_artist_matches"] > 0
    if a_has_matching_aa != b_has_matching_aa:
        return 1 if a_has_matching_aa else -1
        
    a_is_guest = (score_a["album_artist_populated_count"] > 0 and score_a["album_artist_matches"] == 0)
    b_is_guest = (score_b["album_artist_populated_count"] > 0 and score_b["album_artist_matches"] == 0)
    if a_is_guest != b_is_guest:
        return -1 if a_is_guest else 1
        
    if score_a["album_artist_matches"] != score_b["album_artist_matches"]:
        return 1 if score_a["album_artist_matches"] > score_b["album_artist_matches"] else -1

    if score_a["richness_score"] != score_b["richness_score"]:
        return 1 if score_a["richness_score"] > score_b["richness_score"] else -1
        
    return 0


def prompt_user_conflict_resolution(album: str, score_a: dict, score_b: dict) -> str:
    print(clr_yellow(f"\n[!] CONFLICT: Two artists claim ownership of the album '{album}' with equal priority weights."))

    def print_option(label, score_data, color_fn):
        size_mb = score_data["total_size"] / (1024 * 1024)
        print(f"  Option {clr_bold(label)}: Artist {color_fn(repr(score_data['artist_name']))}")
        print(clr_gray(f"    - Track Count: {score_data['track_count']}"))
        print(clr_gray(f"    - Total Size: {size_mb:.2f} MB"))
        print(clr_gray(f"    - Metadata Richness Index: {score_data['richness_score']}"))
        print(clr_gray(f"    - Valid 'Album Artist' Matches: {score_data['album_artist_matches']}/{score_data['track_count']}"))

    print_option("1", score_a, clr_red)
    print_option("2", score_b, clr_green)
    print(f"  Option {clr_bold('3')}: Enter a custom Album Artist name")

    while True:
        choice = _input("\nWhich artist should be the Main Artist? Enter 1, 2, or 3: ").strip()
        if choice == "1":
            return "A"
        if choice == "2":
            return "B"
        if choice == "3":
            custom = _input("Enter custom Album Artist name: ").strip()
            if custom:
                return f"CUSTOM:{custom}"
            print("Name cannot be empty.")
        else:
            print(clr_yellow("Please enter 1, 2, or 3."))


_LOOKALIKE_TABLE = str.maketrans({
    '〜': '~',   # WAVE DASH 〜 → ~
    '～': '~',   # FULLWIDTH TILDE ～ → ~
    '−': '-',   # MINUS SIGN − → -
    '－': '-',   # FULLWIDTH HYPHEN-MINUS － → -
    '‐': '-',   # HYPHEN ‐ → -
    '—': '-',   # EM DASH — → -
    '―': '-',   # HORIZONTAL BAR ― → -
    '·': '.',   # MIDDLE DOT · → .
    '・': '.',   # KATAKANA MIDDLE DOT ・ → .
})

def _normalize_for_comparison(s: str) -> str:
    """Lowercase, strip diacritics, invisible format chars, and fold Unicode lookalikes."""
    s = s.translate(_LOOKALIKE_TABLE)
    # Strip Cf (format) characters: ZWJ, RLM, soft-hyphen, variation selectors, etc.
    s = ''.join(c for c in s if unicodedata.category(c) != 'Cf')
    return ''.join(
        c for c in unicodedata.normalize('NFKD', s)
        if not unicodedata.combining(c)
    ).lower()


def strings_partially_match(a: str, b: str, threshold: float = 0.82) -> bool:
    a_norm = _normalize_for_comparison(a)
    b_norm = _normalize_for_comparison(b)
    # Prefix check: one name is the start of the other (handles "Artist" vs "Artist (feat. X)").
    # SequenceMatcher ratio collapses when lengths differ greatly, so check this first.
    short, long = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    if len(short) >= 4 and long.startswith(short):
        return True
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


def get_track_info_line(path: Path) -> str:
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
        audio = MutagenFile(path)
        duration_str = ""
        if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
            secs = int(audio.info.length)
            duration_str = f"{secs // 60}:{secs % 60:02d}"
        title = path.stem
        meta = get_cached_metadata(path)
        t = meta.get("title")
        if t:
            tnum = meta.get("track_num")
            title = f"{int(tnum):02d} {t}" if (tnum and str(tnum).isdigit()) else t
        info_parts = []
        if duration_str:
            info_parts.append(duration_str)
        info_parts.append(f"{size_mb:.1f} MB")
        meta_tag = clr_gray(f"[{' | '.join(info_parts)}]")
        return f"{title}  {meta_tag}"
    except Exception:
        return path.name


def get_album_track_list(folder: Path) -> list:
    if not folder.exists():
        return []
    tracks = [p for p in folder.rglob("*") if p.is_file() and is_audio_file(p)]
    return sorted(tracks, key=lambda p: p.name)


def find_partial_artist_matches(root: Path, artist_name: str) -> list:
    matches = []
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name in {ZIPS_DIR_NAME, "Orphan"}:
                continue
            if child.name.lower() == artist_name.lower():
                continue
            if strings_partially_match(child.name, artist_name):
                matches.append(child.name)
    except OSError:
        pass
    return matches


def rewrite_albumartist_in_folder(folder: Path, new_artist: str) -> None:
    if not folder.exists():
        return
    for track in folder.rglob("*"):
        if track.is_file() and is_audio_file(track):
            write_albumartist_tag(track, new_artist)


def prompt_artist_merge(album: str, existing_artist: str, new_artist: str, root: Path) -> Optional[str]:
    album_sanitized = sanitize_folder_name(album)

    def list_tracks(artist: str) -> None:
        album_folder = root / artist / album_sanitized
        artist_folder = root / artist
        if album_folder.exists():
            # Show tracks from this specific album under the artist
            scan_folder = album_folder
            show_album_prefix = False
        elif artist_folder.exists():
            # Album not yet organised under this artist — show all their other albums
            # so the user has context for whether these two artists should be merged
            scan_folder = artist_folder
            show_album_prefix = True
        else:
            scan_folder = None
            show_album_prefix = False

        tracks = (
            sorted(
                [p for p in scan_folder.rglob("*") if p.is_file() and is_audio_file(p)],
                key=lambda p: p.name,
            )
            if scan_folder is not None
            else []
        )
        if not tracks:
            print("    (no tracks found)")
        else:
            for i, t in enumerate(tracks, 1):
                line = get_track_info_line(t)
                if show_album_prefix:
                    try:
                        album_part = t.relative_to(artist_folder).parts[0]
                        line = f"[{album_part}] {line}"
                    except (ValueError, IndexError):
                        pass
                print(f"    {i}. {line}")

    print(clr_yellow(f"\n[!] PARTIAL ARTIST MATCH: '{existing_artist}' and '{new_artist}' may be the same artist."))
    print(clr_gray(f"    (Triggered while adding tracks to '{album}')"))
    print(f"\n  {clr_red(existing_artist)} — tracks already in library:")
    list_tracks(existing_artist)
    print(f"\n  {clr_green(new_artist)} — tracks already in library:")
    list_tracks(new_artist)
    print(clr_bold("\nOptions:"))
    print(f"  {clr_bold('1')} = Keep {clr_red(repr(existing_artist))} (update new tracks to match)")
    print(f"  {clr_bold('2')} = Use {clr_green(repr(new_artist))} (update existing tracks to match)")
    print(f"  {clr_bold('3')} = Enter a custom artist name (update ALL tracks in both)")
    print(f"  {clr_gray('Enter')} = Skip for now")

    while True:
        choice = _input("Your choice: ").strip()
        if choice == "1":
            return existing_artist
        if choice == "2":
            return new_artist
        if choice == "3":
            custom = _input("Enter custom artist name: ").strip()
            if custom:
                return custom
            print("Name cannot be empty.")
        elif choice == "":
            return None
        else:
            print(clr_yellow("Please enter 1, 2, 3, or press Enter to skip."))


def find_partial_album_matches(search_dir: Path, album_name: str) -> list:
    matches = []
    if not search_dir.exists():
        return matches
    try:
        for child in search_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name == "Orphan":
                continue
            if child.name.lower() == album_name.lower():
                continue
            if strings_partially_match(child.name, album_name):
                matches.append(child.name)
    except OSError:
        pass
    return matches


def prompt_album_merge(new_album: str, existing_album: str, artist: str, search_dir: Path) -> Optional[str]:
    existing_tracks = get_album_track_list(search_dir / existing_album)

    print(clr_yellow(f"\n[!] PARTIAL ALBUM MATCH detected:"))
    if artist:
        print(f"  Artist: {clr_cyan(repr(artist))}")
    print(f"  Existing album: {clr_red(repr(existing_album))} ({len(existing_tracks)} track(s))")
    for i, t in enumerate(existing_tracks, 1):
        print(f"    {i}. {get_track_info_line(t)}")
    print(f"  New album:      {clr_green(repr(new_album))}")
    print(clr_bold("\nOptions:"))
    print(f"  {clr_bold('1')} = Merge into existing {clr_red(repr(existing_album))} (update new tracks' album tag)")
    print(f"  {clr_bold('2')} = Keep {clr_green(repr(new_album))} as a separate album")
    print(f"  {clr_bold('3')} = Enter a custom album name (update ALL tracks in both)")
    print(f"  {clr_bold('4')} = Ignore ALL partial matches for {clr_green(repr(new_album))} (always keep separate)")
    print(f"  {clr_gray('Enter')} = Skip for now")

    while True:
        choice = _input("Your choice: ").strip()
        if choice == "1":
            return existing_album
        if choice == "2":
            return None
        if choice == "3":
            custom = _input("Enter custom album name: ").strip()
            if custom:
                return f"CUSTOM:{custom}"
            print("Name cannot be empty.")
        if choice == "4":
            return "IGNORE_ALL"
        elif choice == "":
            return None
        else:
            print(clr_yellow("Please enter 1, 2, 3, 4, or press Enter to skip."))


def _resolve_album_partial_match(
    root: Path,
    artist_sanitized: Optional[str],
    album_sanitized: str,
    album_raw: str,
    track_path: Path,
    no_partial_album: bool = False,
    safe: bool = False,
) -> Optional[str]:
    cache_key = (artist_sanitized or "", album_sanitized)
    if cache_key in ALBUM_IGNORE_ALL:
        return None
    if cache_key in ALBUM_MERGE_DECISIONS:
        return ALBUM_MERGE_DECISIONS[cache_key]

    search_dir = (root / artist_sanitized) if artist_sanitized else root
    matches = find_partial_album_matches(search_dir, album_sanitized)

    for existing_album in matches:
        a_low, b_low = sorted([album_sanitized.lower(), existing_album.lower()])
        pair_key = f"{artist_sanitized or ''}|{a_low}|{b_low}"
        if pair_key in ALBUM_PARTIAL_MATCH_ASKED:
            continue
        ALBUM_PARTIAL_MATCH_ASKED.add(pair_key)

        if no_partial_album:
            conflict_key = (artist_sanitized or "", album_sanitized, existing_album)
            if safe:
                SAFE_ALBUM_CONFLICTS.add(conflict_key)
                raise SafeAbortError(
                    f"Album '{album_raw}' conflicts with existing '{existing_album}' "
                    f"(--no-partial-album + --safe: aborting move)"
                )
            logging.debug(
                "Skipping partial album match prompt: '%s' ~ '%s' (--no-partial-album)",
                album_raw, existing_album,
            )
            continue

        decision = prompt_album_merge(album_raw, existing_album, artist_sanitized or "", search_dir)

        if decision == "IGNORE_ALL":
            ALBUM_IGNORE_ALL.add(cache_key)
            save_album_ignore_list(root, ALBUM_IGNORE_ALL)
            logging.info("Ignoring all partial album matches for '%s' (saved).", album_raw)
            return None

        if decision is None:
            continue

        if isinstance(decision, str) and decision.startswith("CUSTOM:"):
            custom_name = decision.split(":", 1)[1]
            canonical = sanitize_folder_name(custom_name)
            for old_track in get_album_track_list(search_dir / existing_album):
                write_album_tag(old_track, custom_name)
            write_album_tag(track_path, custom_name)
        else:
            canonical = sanitize_folder_name(decision)
            write_album_tag(track_path, decision)

        ALBUM_MERGE_DECISIONS[cache_key] = canonical
        return canonical

    return None


def resolve_main_artist(
    meta: dict,
    root: Path,
    current_track: Path,
    no_partial_artist: bool = False,
    safe: bool = False,
) -> str:
    album = meta["album"]
    if not album:
        return sanitize_folder_name(meta["albumartist"] or meta["artist"] or "Unknown Artist")
        
    current_artist = sanitize_folder_name(meta["albumartist"] or meta["artist"] or "Unknown Artist")
    cache_key = (album, current_artist)
    
    # If album has no artist info, prompt user to provide one
    if current_artist == "Unknown Artist":
        unknown_key = (album, "UNKNOWN")
        if unknown_key not in ALBUM_MAIN_ARTIST_CACHE:
            user_artist = _input(
                f"\nAlbum '{album}' has no artist information.\n"
                f"Enter artist name for this album: "
            ).strip()
            if user_artist:
                main_artist = sanitize_folder_name(user_artist)
                write_albumartist_tag(current_track, user_artist)
                ALBUM_MAIN_ARTIST_CACHE[unknown_key] = main_artist
                return main_artist
        else:
            return ALBUM_MAIN_ARTIST_CACHE[unknown_key]
    
    if cache_key in ALBUM_MAIN_ARTIST_CACHE:
        cached = ALBUM_MAIN_ARTIST_CACHE[cache_key]
        if cached != current_artist:
            # A previous merge decision unified this artist to a different name;
            # propagate that to this track's tag so it doesn't look like an orphan.
            write_albumartist_tag(current_track, cached)
        return cached
        
    album_sanitized = sanitize_folder_name(album)
    candidates = {current_artist: [current_track]}
    
    try:
        for artist_dir in root.iterdir():
            if artist_dir.is_dir() and artist_dir.name not in {ZIPS_DIR_NAME, "Orphan"}:
                # Only consider exact artist folder matches, not variants with suffixes
                # This prevents old conflict-created folders like "artist_Variant1" from interfering
                album_dir = artist_dir / album_sanitized
                if album_dir.exists():
                    artist_name = artist_dir.name
                    # Skip folders that have artist-variant suffixes (e.g., "Artist_Variant")
                    # These are leftover from previous conflicts and should not be candidates
                    if "_" in artist_name and not artist_name == current_artist:
                        continue
                    if artist_name not in candidates:
                        candidates[artist_name] = []
                    for f in album_dir.rglob("*"):
                        if f.is_file() and is_audio_file(f):
                            if f.resolve() != current_track.resolve():
                                candidates[artist_name].append(f)
    except OSError:
        pass
        
    if len(candidates) == 1:
        best_artist = list(candidates.keys())[0]

        # If auto-detected artist differs from track metadata, skip auto-move and require manual action
        if best_artist != current_artist and best_artist != "Unknown Artist":
            logging.warning(
                "Album '%s' has single matching folder ('%s') but track metadata shows '%s'. "
                "Fix track metadata or rename folder to resolve. Skipping for now.",
                album, best_artist, current_artist
            )
            # Don't cache or auto-move - return None to trigger manual handling
            return None
        
        ALBUM_MAIN_ARTIST_CACHE[cache_key] = best_artist

        # Still check for partial matches with existing root folders even in the single-candidate
        # case — e.g. "Limonène" tracks for a new album when "Limonene" folder already exists
        # for other albums (that folder never enters candidates because it lacks THIS album).
        best_lower = best_artist.lower()
        partial_artist_matches = find_partial_artist_matches(root, best_artist)
        for other_artist in partial_artist_matches:
            other_lower = other_artist.lower()
            pair_key = (min(best_lower, other_lower), max(best_lower, other_lower))
            if pair_key in ARTIST_PARTIAL_MATCH_ASKED:
                continue
            ARTIST_PARTIAL_MATCH_ASKED.add(pair_key)
            if no_partial_artist:
                if safe:
                    SAFE_ARTIST_CONFLICTS.add(pair_key)
                    raise SafeAbortError(
                        f"Artist '{best_artist}' conflicts with existing '{other_artist}' "
                        f"(--no-partial-artist + --safe: aborting move)"
                    )
                logging.debug("Skipping partial artist match: '%s' ~ '%s' (--no-partial-artist)", best_artist, other_artist)
                continue
            decision = prompt_artist_merge(album, other_artist, best_artist, root)
            if decision:
                unified = sanitize_folder_name(decision)
                rewrite_albumartist_in_folder(root / other_artist, decision)
                rewrite_albumartist_in_folder(root / best_artist, decision)
                best_artist = unified
                ALBUM_MAIN_ARTIST_CACHE[cache_key] = unified

        return best_artist

    scores = {}
    for artist_name, tracks in candidates.items():
        scores[artist_name] = score_track_group(tracks, artist_name)

    candidate_keys = list(candidates.keys())
    best_artist = candidate_keys[0]
    custom_albumartist = None
    for artist_name in candidate_keys[1:]:
        decision = evaluate_album_priority(scores[artist_name], scores[best_artist])
        if decision == 1:
            best_artist = artist_name
        elif decision == 0:
            user_pick = prompt_user_conflict_resolution(album, scores[artist_name], scores[best_artist])
            if user_pick == "A":
                best_artist = artist_name
            elif user_pick.startswith("CUSTOM:"):
                custom_albumartist = user_pick.split(":", 1)[1]
                best_artist = sanitize_folder_name(custom_albumartist)

    if custom_albumartist:
        all_tracks = {t for tracks in candidates.values() for t in tracks}
        write_albumartist_for_tracks(all_tracks, custom_albumartist)
        ALBUM_MAIN_ARTIST_CACHE.clear()

    ALBUM_MAIN_ARTIST_CACHE[cache_key] = best_artist

    # Check for partial name matches among candidates (catches accent/diacritic variants
    # like "Limonene" vs "Limonène" where the incoming track has no folder yet)
    best_lower = best_artist.lower()
    for candidate_artist in candidate_keys:
        if candidate_artist == best_artist:
            continue
        candidate_lower = candidate_artist.lower()
        pair_key = (min(best_lower, candidate_lower), max(best_lower, candidate_lower))
        if pair_key in ARTIST_PARTIAL_MATCH_ASKED:
            continue
        if not strings_partially_match(candidate_artist, best_artist):
            continue
        ARTIST_PARTIAL_MATCH_ASKED.add(pair_key)
        if no_partial_artist:
            if safe:
                SAFE_ARTIST_CONFLICTS.add(pair_key)
                raise SafeAbortError(
                    f"Artist '{best_artist}' conflicts with candidate '{candidate_artist}' "
                    f"(--no-partial-artist + --safe: aborting move)"
                )
            logging.debug("Skipping partial artist match: '%s' ~ '%s' (--no-partial-artist)", best_artist, candidate_artist)
            continue
        decision = prompt_artist_merge(album, candidate_artist, best_artist, root)
        if decision:
            unified = sanitize_folder_name(decision)
            if (root / best_artist).exists():
                rewrite_albumartist_in_folder(root / best_artist, decision)
            if (root / candidate_artist).exists():
                rewrite_albumartist_in_folder(root / candidate_artist, decision)
            if candidate_artist == current_artist:
                write_albumartist_tag(current_track, decision)
            best_artist = unified
            ALBUM_MAIN_ARTIST_CACHE[cache_key] = unified

    # Check for partial artist matches from existing root-level artist folders
    best_lower = best_artist.lower()
    partial_artist_matches = find_partial_artist_matches(root, best_artist)
    for other_artist in partial_artist_matches:
        other_lower = other_artist.lower()
        pair_key = (min(best_lower, other_lower), max(best_lower, other_lower))
        if pair_key in ARTIST_PARTIAL_MATCH_ASKED:
            continue
        ARTIST_PARTIAL_MATCH_ASKED.add(pair_key)
        if no_partial_artist:
            if safe:
                SAFE_ARTIST_CONFLICTS.add(pair_key)
                raise SafeAbortError(
                    f"Artist '{best_artist}' conflicts with existing folder '{other_artist}' "
                    f"(--no-partial-artist + --safe: aborting move)"
                )
            logging.debug("Skipping partial artist match: '%s' ~ '%s' (--no-partial-artist)", best_artist, other_artist)
            continue
        decision = prompt_artist_merge(album, other_artist, best_artist, root)
        if decision:
            unified = sanitize_folder_name(decision)
            rewrite_albumartist_in_folder(root / other_artist, decision)
            rewrite_albumartist_in_folder(root / best_artist, decision)
            best_artist = unified
            ALBUM_MAIN_ARTIST_CACHE[cache_key] = unified

    return best_artist


def extract_album_art(source_path: Path) -> Optional[Tuple[bytes, str]]:
    try:
        audio = MutagenFile(source_path)
        if audio is None:
            return None
        
        if hasattr(audio, 'pictures') and audio.pictures:
            pic = audio.pictures[0]
            return pic.data, pic.mime
        
        if audio.tags and hasattr(audio.tags, 'getall'):
            apics = audio.tags.getall('APIC')
            if apics:
                return apics[0].data, apics[0].mime
        
        if audio.tags:
            for key in audio.tags.keys():
                if key.lower() == 'metadata_block_picture':
                    for b64_data in audio.tags[key]:
                        try:
                            pic_bytes = base64.b64decode(b64_data)
                            pic = Picture(pic_bytes)
                            return pic.data, pic.mime
                        except Exception:
                            continue
                            
        if audio.tags and 'covr' in audio.tags:
            covr_list = audio.tags['covr']
            if covr_list:
                data = covr_list[0]
                mime = "image/jpeg"
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime = "image/png"
                return bytes(data), mime
    except Exception as e:
        logging.debug("Failed to extract album art from %s: %s", source_path, e)
    return None


def inject_album_art(mp3_path: Path, art_data: bytes, mime_type: str) -> None:
    try:
        try:
            tags = ID3(str(mp3_path))
        except Exception:
            tags = ID3()
        
        tags.add(APIC(
            encoding=3,  
            mime=mime_type,
            type=3,      
            desc='Cover',
            data=art_data
        ))
        tags.save(str(mp3_path), v2_version=3)
    except Exception as e:
        logging.warning("Failed to inject album art into %s: %s", mp3_path, e)


def has_album_art(mp3_path: Path) -> bool:
    return extract_album_art(mp3_path) is not None


def write_albumartist_tag(path: Path, albumartist: str) -> None:
    if _DRY_RUN:
        logging.info("DRY-RUN: would set albumartist='%s' on %s", albumartist, path.name)
        return
    try:
        # Get original mtime before modification
        try:
            stat_before = path.stat()
            mtime_sec = stat_before.st_mtime
            atime_sec = stat_before.st_atime
        except OSError:
            mtime_sec = None
            atime_sec = None

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        audio["albumartist"] = albumartist
        audio.save()
        
        # Clear metadata cache for this file
        try:
            resolved = path.resolve()
            METADATA_CACHE.pop(resolved, None)
        except OSError:
            pass
        
        # Restore original mtime (in seconds) to prevent re-detection as "modified"
        if mtime_sec is not None and atime_sec is not None:
            try:
                os.utime(path, (atime_sec, mtime_sec))
            except OSError as e:
                logging.debug("Could not restore mtime for %s: %s", path.name, e)
                
        logging.info("Updated album artist for %s -> %s", path.name, albumartist)
    except Exception as exc:
        logging.warning("Failed to write album artist to %s: %s", path.name, exc)


def write_albumartist_for_tracks(tracks: Iterable[Path], albumartist: str) -> None:
    seen = set()
    for track in tracks:
        try:
            resolved = track.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        write_albumartist_tag(track, albumartist)


def write_album_tag(path: Path, album: str) -> None:
    if _DRY_RUN:
        logging.info("DRY-RUN: would set album='%s' on %s", album, path.name)
        return
    try:
        try:
            stat_before = path.stat()
            mtime_sec = stat_before.st_mtime
            atime_sec = stat_before.st_atime
        except OSError:
            mtime_sec = None
            atime_sec = None

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        audio["album"] = album
        audio.save()

        try:
            resolved = path.resolve()
            METADATA_CACHE.pop(resolved, None)
        except OSError:
            pass

        if mtime_sec is not None and atime_sec is not None:
            try:
                os.utime(path, (atime_sec, mtime_sec))
            except OSError as e:
                logging.debug("Could not restore mtime for %s: %s", path.name, e)

        logging.info("Updated album tag for %s -> %s", path.name, album)
    except Exception as exc:
        logging.warning("Failed to write album tag to %s: %s", path.name, exc)


def ensure_albumartist(path: Path, meta: dict) -> dict:
    if meta.get("albumartist"):
        return meta

    try:
        path_key = str(path.resolve())
    except OSError:
        path_key = str(path)

    if path_key in MISSING_ALBUMARTIST_ASKED:
        return meta

    album = meta.get("album") or ""

    if album and album in ALBUMARTIST_ALL_APPROVED:
        value = ALBUMARTIST_ALL_APPROVED[album]
        write_albumartist_tag(path, value)
        meta = dict(meta)
        meta["albumartist"] = value
        if not meta.get("artist"):
            meta["artist"] = value
        MISSING_ALBUMARTIST_ASKED.add(path_key)
        return meta

    title = meta.get("title") or path.stem
    track_artist = meta.get("artist") or ""
    print(f"\nTrack '{title}' (album: '{album or 'Unknown'}') has no album artist tag.")
    if track_artist:
        print(f"  a = Use track artist '{track_artist}'")
    print(f"  Enter = Skip")

    while True:
        value = _input("Album artist (name / 'a' / Enter to skip): ").strip()
        if not value:
            MISSING_ALBUMARTIST_ASKED.add(path_key)
            return meta
        if value.lower() == "a" and track_artist:
            value = track_artist
            break
        if value.lower() == "a":
            print("No track artist available.")
        else:
            break

    apply_all = False
    if album:
        ans = _input(
            f"Apply '{value}' to all remaining tracks in '{album}' without asking again? [y/N]: "
        ).strip().lower()
        if ans in ("y", "yes"):
            apply_all = True

    write_albumartist_tag(path, value)
    meta = dict(meta)
    meta["albumartist"] = value
    if not meta.get("artist"):
        meta["artist"] = value
    MISSING_ALBUMARTIST_ASKED.add(path_key)

    if apply_all and album:
        ALBUMARTIST_ALL_APPROVED[album] = value

    return meta


def apply_external_art(mp3_path: Path, img_path: Path) -> None:
    mime_type = "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
    try:
        with open(img_path, "rb") as f:
            art_data = f.read()
        inject_album_art(mp3_path, art_data, mime_type)
        logging.info("Applied artwork %s to %s", img_path.name, mp3_path.name)
    except Exception as e:
        logging.error("Failed to apply artwork to %s: %s", mp3_path.name, e)


def get_destination_info(
    root: Path,
    path: Path,
    meta: dict,
    hierarchy: bool,
    no_partial_album: bool = False,
    no_partial_artist: bool = False,
    safe: bool = False,
) -> Tuple[Path, str, bool]:
    album = meta["album"] or "Unknown Album"
    album_sanitized = sanitize_folder_name(album)

    if not hierarchy:
        canonical_album = _resolve_album_partial_match(root, None, album_sanitized, album, path, no_partial_album, safe)
        if canonical_album is not None:
            album_sanitized = canonical_album
        return root / album_sanitized, path.stem, False

    main_artist_sanitized = resolve_main_artist(meta, root, path, no_partial_artist, safe)

    # If resolve_main_artist returns None (e.g., single-match with metadata mismatch),
    # fall back to using track's own artist metadata
    if main_artist_sanitized is None:
        main_artist_sanitized = sanitize_folder_name(meta.get("albumartist") or meta.get("artist") or "Unknown Artist")

    # Re-read metadata: resolve_main_artist may have rewritten the albumartist tag
    # (e.g. after a merge decision). Without this, the orphan check below uses the
    # stale pre-merge value and incorrectly sends tracks to Orphan/.
    meta = get_cached_metadata(path)

    albumartist_raw = meta.get("albumartist") or ""
    artist_raw = meta.get("artist") or ""
    track_albumartist_sanitized = sanitize_folder_name(albumartist_raw) if albumartist_raw else ""
    track_artist_sanitized = sanitize_folder_name(artist_raw) if artist_raw else ""
    
    is_orphan = False
    if main_artist_sanitized and track_albumartist_sanitized:
        if track_albumartist_sanitized != main_artist_sanitized:
            is_orphan = True

    if is_orphan:
        dest_dir = root / main_artist_sanitized / "Orphan"
        target_stem = path.stem
        return dest_dir, target_stem, True

    # Check for partial album name match under the artist folder
    canonical_album = _resolve_album_partial_match(root, main_artist_sanitized, album_sanitized, album, path, no_partial_album, safe)
    if canonical_album is not None:
        album_sanitized = canonical_album

    dest_dir = root / main_artist_sanitized / album_sanitized

    # Unified Multi-Disc Determination Logic
    _mdc_key = (main_artist_sanitized, album_sanitized)
    multi_disc = ALBUM_MULTI_DISC_CACHE.get(_mdc_key, False)
    if not multi_disc:
        # Fallback to check if a Disc > 1 folder exists locally
        if dest_dir.exists():
            try:
                for child in dest_dir.iterdir():
                    if child.is_dir() and re.match(r'^Disc\s+([2-9]|\d{2,})$', child.name, re.IGNORECASE):
                        multi_disc = True
                        ALBUM_MULTI_DISC_CACHE[_mdc_key] = True
                        break
            except OSError:
                pass

    if multi_disc:
        d_num_safe = (meta.get("disc_num") or "").strip()
        d_num_safe = str(int(d_num_safe)) if d_num_safe.isdigit() else "1"
        dest_dir = dest_dir / sanitize_folder_name(f"Disc {d_num_safe}")

    raw_title = meta["title"] or path.stem
    extracted_track, cleaned_title = extract_track_and_title(raw_title)
    title_sanitized = sanitize_file_name(cleaned_title)

    t_num = meta["track_num"]
    final_track_num = None
    
    if t_num and t_num.isdigit():
        final_track_num = int(t_num)
    elif extracted_track and extracted_track.isdigit():
        final_track_num = int(extracted_track)

    if final_track_num is not None:
        target_stem = f"{final_track_num:02d} {title_sanitized}"
    else:
        target_stem = title_sanitized

    return dest_dir, target_stem, False


def unique_destination_path(destination_dir: Path, stem: str, suffix: str) -> Path:
    candidate = destination_dir / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate

    counter = 1
    while True:
        candidate = destination_dir / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_associated_images(source_dir: Path, dest_dir: Path, dry_run: bool) -> None:
    if source_dir.resolve() == dest_dir.resolve():
        return
    try:
        for p in source_dir.iterdir():
            if is_image_file(p):
                target = unique_destination_path(dest_dir, p.stem, p.suffix)
                if dry_run:
                    logging.info("DRY-RUN: would move album art %s -> %s", p.name, dest_dir.name)
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                logging.info("Moved associated album art: %s -> %s", p.name, target.parent.name)
    except OSError:
        pass


def move_file(path: Path, destination_dir: Path, target_stem: str, root: Path, dry_run: bool) -> Path:
    if not dry_run:
        destination_dir.mkdir(parents=True, exist_ok=True)

    canonical = destination_dir / f"{target_stem}{path.suffix}"

    # If the canonical target already exists and the source is already sitting in the
    # destination folder, the source is a stale duplicate (wrong filename format, same
    # track). Delete it instead of creating a "(1)" copy.
    if canonical.exists() and canonical.resolve() != path.resolve():
        if path.parent.resolve() == destination_dir.resolve():
            logging.warning(
                "Duplicate in place: '%s' matches existing '%s' — removing source.",
                path.name, canonical.name,
            )
            if not dry_run:
                path.unlink()
            if "Orphan" not in canonical.parts:
                RECENT_ALBUM_DIRS.appendleft(destination_dir)
            return canonical

    destination = unique_destination_path(destination_dir, target_stem, path.suffix)

    if not within_root(destination, root):
        raise RuntimeError(f"Refusing to move outside root: {destination}")

    if path.resolve() == destination.resolve():
        return destination

    if dry_run:
        logging.info("DRY-RUN: move %s -> %s", path, destination)
        return destination

    shutil.move(str(path), str(destination))
    logging.info("Moved %s -> %s", path, destination)
    if "Orphan" not in destination.parts:
        RECENT_ALBUM_DIRS.appendleft(destination_dir)
    return destination


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def prompt_for_conversion(album: str, path: Path, approved_conversions: set[str]) -> bool:
    if album in approved_conversions:
        return True

    while True:
        answer = _input(
            f"Convert non-MP3 file to MP3?\nAlbum: {album}\nFile:  {path.name}\nContinue [y/N/a (all for this album)]: "
        ).strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"a", "all"}:
            approved_conversions.add(album)
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Please answer y, n, or a.")


def prompt_for_art(album: str, mp3_path: Path, img_path: Path, approved_art: set[str]) -> bool:
    if album in approved_art:
        return True

    while True:
        ans = _input(
            f"\nApply album art to missing track?\nAlbum: {album}\nTrack: {mp3_path.name}\nImage: {img_path.name}\nContinue [y/N/a (all for this album)]: "
        ).strip().lower()
        if ans in {'y', 'yes'}:
            return True
        if ans in {'a', 'all'}:
            approved_art.add(album)
            return True
        if ans in {'n', 'no', ''}:
            return False
        print("Please answer y, n, or a.")


def convert_to_mp3(source: Path, destination_dir: Path, target_stem: str, root: Path, dry_run: bool, meta: dict) -> Path:
    final_output = destination_dir / f"{target_stem}{MP3_SUFFIX}"

    if not within_root(final_output, root):
        raise RuntimeError(f"Refusing to write outside root: {final_output}")

    if dry_run:
        logging.info("DRY-RUN: would convert %s -> %s", source.name, final_output)
        return final_output

    destination_dir.mkdir(parents=True, exist_ok=True)
    final_output = unique_destination_path(destination_dir, target_stem, MP3_SUFFIX)
    art_info = extract_album_art(source)

    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or is not available on PATH")

    temp_output = final_output.with_name(f".{final_output.stem}.tmp.mp3")
    if temp_output.exists():
        temp_output.unlink()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i", str(source),
        "-map", "0:a",          
        "-c:a", "libmp3lame",
        "-q:a", "2",
        "-id3v2_version", "3",  
    ]

    if meta.get("title"): command.extend(["-metadata", f"title={meta['title']}"])
    if meta.get("artist"): command.extend(["-metadata", f"artist={meta['artist']}"])
    if meta.get("albumartist"): command.extend(["-metadata", f"album_artist={meta['albumartist']}"])
    if meta.get("album"): command.extend(["-metadata", f"album={meta['album']}"])
    if meta.get("track_raw"): command.extend(["-metadata", f"track={meta['track_raw']}"])
    if meta.get("disc_raw"): command.extend(["-metadata", f"disc={meta['disc_raw']}"])

    command.append(str(temp_output))

    try:
        result = subprocess.run(command, check=False, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            if result.stderr:
                logging.error("ffmpeg error for %s:\n%s", source.name, result.stderr.strip())
            result.check_returncode()
        elif result.stderr:
            logging.debug("ffmpeg diagnostics for %s: %s", source.name, result.stderr.strip())
        temp_output.replace(final_output)

        if art_info:
            inject_album_art(final_output, art_info[0], art_info[1])

        if final_output.stat().st_size < 1024:
            logging.error(
                "Converted file appears corrupt (<1 KB): %s — source kept", final_output.name
            )
            return final_output

        source.unlink()
        logging.info("Converted %s -> %s and retained album art", source.name, final_output.name)
        if "Orphan" not in final_output.parts:
            RECENT_ALBUM_DIRS.appendleft(final_output.parent)
        return final_output
    except Exception:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError:
                pass
        raise


def safe_extract_zip(archive: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    root_resolved = destination_dir.resolve()

    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            member_path = (destination_dir / member.filename).resolve()
            try:
                member_path.relative_to(root_resolved)
            except ValueError as exc:
                raise RuntimeError(
                    f"Unsafe zip entry rejected: {member.filename!r}"
                ) from exc

            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue

            member_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(member_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def unique_directory_path(parent: Path, name: str) -> Path:
    candidate = parent / name
    if not candidate.exists():
        return candidate

    counter = 1
    while True:
        candidate = parent / f"{name} ({counter})"
        if not candidate.exists():
            return candidate
        counter += 1


def archive_storage_dir(root: Path) -> Path:
    return root / ZIPS_DIR_NAME


def archive_extraction_dir(root: Path, archive_stem: str) -> Path:
    return archive_storage_dir(root) / archive_stem


def move_archive_into_zips(archive: Path, root: Path, dry_run: bool) -> Path:
    zips_root = archive_storage_dir(root)
    destination = zips_root / archive.name

    if archive.resolve() == destination.resolve():
        return destination

    if dry_run:
        logging.info("DRY-RUN: would move archive %s -> %s", archive.name, destination)
        return destination

    zips_root.mkdir(parents=True, exist_ok=True)
    destination = unique_destination_path(zips_root, archive.stem, archive.suffix)

    shutil.move(str(archive), str(destination))
    logging.info("Moved archive %s -> %s", archive, destination)
    return destination


def rename_single_nested_music_folder(extraction_root: Path, album: str, root: Path, dry_run: bool, hierarchy: bool) -> None:
    if hierarchy:
        return

    album_folder_name = sanitize_folder_name(album)

    music_files = [p for p in extraction_root.rglob("*") if p.is_file() and is_audio_file(p)]
    if not music_files:
        return

    top_level_names = set()
    for file_path in music_files:
        rel_parts = file_path.relative_to(extraction_root).parts
        if len(rel_parts) >= 2:
            top_level_names.add(rel_parts[0])

    if len(top_level_names) != 1:
        return

    nested_name = next(iter(top_level_names))
    nested_dir = extraction_root / nested_name
    if not nested_dir.is_dir():
        return

    target_dir = extraction_root / album_folder_name
    if nested_dir.name == album_folder_name:
        return
    if target_dir.exists():
        logging.warning(
            "Cannot rename extracted folder to %s because that path already exists",
            target_dir,
        )
        return

    if dry_run:
        logging.info("DRY-RUN: rename %s -> %s", nested_dir, target_dir)
        return

    nested_dir.rename(target_dir)
    logging.info("Renamed extracted folder %s -> %s", nested_dir, target_dir)


def process_archive(
    archive_path: Path,
    root: Path,
    dry_run: bool,
    processed_archives: Dict[Path, FileStamp],
    hierarchy: bool,
) -> None:
    try:
        stat_result = archive_path.stat()
    except OSError:
        return

    stamp = FileStamp(size=stat_result.st_size, mtime_ns=stat_result.st_mtime_ns)
    previous = processed_archives.get(archive_path)
    if previous == stamp:
        return

    archive_path = move_archive_into_zips(archive_path, root, dry_run)
    extraction_root = archive_extraction_dir(root, archive_path.stem)

    if processed_archives.get(archive_path) == stamp and extraction_root.exists():
        return

    if dry_run:
        logging.info("DRY-RUN: extract %s -> %s", archive_path, extraction_root)
        processed_archives[archive_path] = stamp
        return

    if extraction_root.exists():
        # Already extracted in a previous session; audio files there will be
        # processed normally by the track handler — don't extract again.
        logging.debug(
            "Archive %s already extracted to %s; skipping re-extraction.",
            archive_path.name, extraction_root.name,
        )
        processed_archives[archive_path] = stamp
        return

    try:
        safe_extract_zip(archive_path, extraction_root)
    except Exception as exc:
        logging.error("Failed to extract %s: %s", archive_path, exc)
        return

    logging.info("Extracted %s -> %s", archive_path.name, extraction_root.name)

    albums = set()
    for path in extraction_root.rglob("*"):
        if path.is_file() and is_audio_file(path):
            meta = get_cached_metadata(path)
            if meta["album"]:
                albums.add(meta["album"])

    if len(albums) == 1:
        album_name = next(iter(albums))
        rename_single_nested_music_folder(extraction_root, album_name, root, dry_run, hierarchy)

    processed_archives[archive_path] = stamp


def process_track(
    path: Path,
    root: Path,
    dry_run: bool,
    pending_tracks: Dict[Path, PendingEntry],
    approved_conversions: set[str],
    approved_art: set[str],
    hierarchy: bool,
    skip_art: bool,
    no_convert: bool = False,
    no_partial_album: bool = False,
    no_partial_artist: bool = False,
    safe: bool = False,
) -> None:
    try:
        stat_result = path.stat()
    except OSError:
        pending_tracks.pop(path, None)
        return

    current_stamp = FileStamp(
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
    )

    entry = pending_tracks.get(path)
    if entry is None or entry.stamp != current_stamp:
        now_ts = time.monotonic()
        first_ever = entry.first_ever_seen if entry is not None else now_ts
        pending_tracks[path] = PendingEntry(
            first_seen=now_ts,
            first_ever_seen=first_ever,
            stamp=current_stamp,
            asked_conversion=entry.asked_conversion if entry is not None else False,
        )
        return

    meta = get_cached_metadata(path)
    album = meta["album"]
    if not album:
        pending_tracks.pop(path, None)
        return

    meta = ensure_albumartist(path, meta)
    try:
        destination_dir, target_stem, is_orphan = get_destination_info(
            root, path, meta, hierarchy, no_partial_album, no_partial_artist, safe
        )
    except SafeAbortError as exc:
        logging.warning("Safe-abort: held back '%s' — %s", path.name, exc)
        pending_tracks.pop(path, None)
        return

    if not within_root(destination_dir, root):
        logging.warning("Skipping unsafe destination for album %r", album)
        pending_tracks.pop(path, None)
        return

    final_mp3 = None
    if is_audio_file(path) and path.suffix.lower() == MP3_SUFFIX:
        if path.parent.resolve() != destination_dir.resolve() or path.stem != target_stem:
            if not skip_art:
                move_associated_images(path.parent, destination_dir, dry_run)
            final_mp3 = move_file(path, destination_dir, target_stem, root, dry_run)
        else:
            # File is already in correct location - skip re-processing
            final_mp3 = path
    else:
        if no_convert:
            logging.warning("Skipping non-MP3 file (--no-convert): %s", path.name)
            pending_tracks.pop(path, None)
            return

        if not entry.asked_conversion:
            if not prompt_for_conversion(album, path, approved_conversions):
                logging.info("Skipped conversion for %s", path.name)
                entry.asked_conversion = True
                return
            entry.asked_conversion = True

        try:
            latest = path.stat()
        except OSError:
            pending_tracks.pop(path, None)
            return

        latest_stamp = FileStamp(size=latest.st_size, mtime_ns=latest.st_mtime_ns)
        if latest_stamp != entry.stamp:
            pending_tracks[path] = PendingEntry(first_seen=time.monotonic(), stamp=latest_stamp)
            return

        try:
            if not skip_art:
                move_associated_images(path.parent, destination_dir, dry_run)
            final_mp3 = convert_to_mp3(path, destination_dir, target_stem, root, dry_run, meta)
        except Exception as exc:
            logging.error("Failed to process %s: %s", path.name, exc)
            return

    if is_orphan and final_mp3:
        logging.warning("Track '%s' has been orphaned at: %s", final_mp3.name, destination_dir)

    if final_mp3 and final_mp3.exists() and not skip_art:
        if not has_album_art(final_mp3):
            try:
                dest_images = [p for p in destination_dir.iterdir() if is_image_file(p)]
                if dest_images:
                    img_path = dest_images[0]
                    if prompt_for_art(album, final_mp3, img_path, approved_art):
                        if not dry_run:
                            apply_external_art(final_mp3, img_path)
            except OSError:
                pass

    pending_tracks.pop(path, None)


def prompt_root_image_destination(img_path: Path, root: Path) -> Optional[Path]:
    """Ask user which album a stray root-level image belongs to.

    Shows the last 5 unique recent album destinations numbered 1-5, plus a
    custom fuzzy-search option.  Returns the chosen album folder, or None if
    the user skips.
    """
    # Collect up to 5 unique recent dirs that still exist
    seen: list[Path] = []
    for d in RECENT_ALBUM_DIRS:
        if d not in seen and d.exists():
            seen.append(d)
        if len(seen) == 5:
            break

    print(f"\n[?] Stray image at root: '{img_path.name}'")
    print("    Which album does it belong to?\n")

    if seen:
        for i, d in enumerate(seen, 1):
            try:
                rel = d.relative_to(root)
            except ValueError:
                rel = d
            print(f"  {i} = {rel}")
    else:
        print("  (No recent album destinations recorded yet)")

    print(f"  c = Custom search")
    print(f"  s = Skip")

    # Build candidate folder list once — avoids repeated rglob on every 'c' press
    candidates: list[Path] = []
    try:
        for p in root.rglob("*"):
            if p.is_dir() and p != root:
                rel_parts = p.relative_to(root).parts
                if len(rel_parts) <= 3:
                    candidates.append(p)
    except OSError:
        pass

    while True:
        raw = _input("\nChoice: ").strip().lower()

        if raw == "s" or raw == "":
            return None

        do_custom_search = raw == "c"
        if do_custom_search:
            query = _input("Search albums (fuzzy): ").strip()
            if not query:
                continue

            # Fuzzy-rank by folder name match — score each candidate once
            query_norm = _normalize_for_comparison(query)
            scored = sorted(
                ((difflib.SequenceMatcher(None, query_norm, _normalize_for_comparison(f.name)).ratio(), f)
                 for f in candidates),
                reverse=True,
            )
            matches = [f for s, f in scored if s >= 0.4][:10]

            if not matches:
                print("  No matches found. Try a different query.")
                continue

            if len(matches) == 1:
                dest = matches[0]
                try:
                    rel = dest.relative_to(root)
                except ValueError:
                    rel = dest
                confirm = _input(f"  Use '{rel}'? [Y/n]: ").strip().lower()
                if confirm in ("", "y"):
                    return dest
                continue

            print("\n  Multiple matches:")
            for i, m in enumerate(matches, 1):
                try:
                    rel = m.relative_to(root)
                except ValueError:
                    rel = m
                print(f"    {i} = {rel}")
            pick = _input("  Enter number (or Enter to cancel): ").strip()
            if pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(matches):
                    return matches[idx]
            continue

        if seen and raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(seen):
                return seen[idx]

        print("  Invalid choice.")


def process_image(
    img_path: Path,
    root: Path,
    dry_run: bool,
    pending: Dict[Path, PendingEntry],
    approved_art: set[str],
    skip_art: bool,
    hierarchy: bool = False,
) -> None:
    if skip_art:
        pending.pop(img_path, None)
        return

    try:
        if not img_path.exists():
            pending.pop(img_path, None)
            return

        # Root-level stray image handling (hierarchy mode)
        if hierarchy and img_path.parent.resolve() == root.resolve():
            dest_dir = prompt_root_image_destination(img_path, root)
            if dest_dir is not None and not dry_run:
                dest_path = dest_dir / img_path.name
                if not dest_path.exists():
                    shutil.move(str(img_path), str(dest_path))
                    logging.info("Moved stray image '%s' -> '%s'", img_path.name, dest_path)
                else:
                    logging.warning("Image '%s' already exists in '%s'; skipping.", img_path.name, dest_dir)
            return

        mp3_peers = [p for p in img_path.parent.iterdir() if p.suffix.lower() == MP3_SUFFIX]
        for mp3 in mp3_peers:
            if not has_album_art(mp3):
                meta = get_cached_metadata(mp3)
                album_name = meta["album"] or img_path.parent.name

                if prompt_for_art(album_name, mp3, img_path, approved_art):
                    if not dry_run:
                        apply_external_art(mp3, img_path)

    except OSError:
        pass
    finally:
        pending.pop(img_path, None)


def cleanup_stale_folders(
    root: Path,
    prompted_dirs: set,
    dry_run: bool,
    ignore_list: Optional[set] = None,
    cooldown: float = DEFAULT_CLEANUP_COOLDOWN_SECONDS,
    folder_seen_times: Optional[Dict[Path, float]] = None,
) -> None:
    if ignore_list is None:
        ignore_list = set()
    if folder_seen_times is None:
        folder_seen_times = {}
    now = time.monotonic()
    zips_dir = archive_storage_dir(root).resolve()
    root_resolved = root.resolve()

    for dirpath, _, _ in os.walk(root, topdown=False):
        current = Path(dirpath).resolve()

        if current == root_resolved:
            continue
        try:
            if zips_dir in current.parents or current == zips_dir:
                continue
        except Exception:
            pass

        try:
            rel_key = str(current.relative_to(root_resolved))
        except ValueError:
            rel_key = str(current)

        if rel_key in ignore_list:
            continue

        try:
            contents = list(current.iterdir())
        except OSError:
            continue

        if not contents:
            logging.info("Cleaning up empty folder: %s", current.name)
            if not dry_run:
                try:
                    current.rmdir()
                except OSError:
                    pass
        else:
            try:
                all_files = [f for f in current.rglob("*") if f.is_file()]
            except OSError:
                continue
            has_media = any(is_audio_file(f) or is_zip_file(f) or is_image_file(f) for f in all_files)
            if has_media:
                # Media present — reset any cooldown timer for this folder
                folder_seen_times.pop(current, None)
                continue

            if current in prompted_dirs:
                continue

            # Apply cooldown: only prompt after the folder has been media-free for `cooldown` seconds
            if current not in folder_seen_times:
                folder_seen_times[current] = now
                continue
            if now - folder_seen_times[current] < cooldown:
                continue

            try:
                rel_display = str(current.relative_to(root))
            except ValueError:
                rel_display = current.name

            non_media_files = sorted(f.name for f in all_files)

            print(f"\nFolder '{rel_display}' contains only non-media files:")
            for name in non_media_files:
                print(f"  {name}")
            print("Delete this folder entirely?")
            ans = _input("  [y/N/i (add to ignore list)]: ").strip().lower()

            if ans in {"y", "yes"}:
                try:
                    # Re-scan after prompt in case media appeared while waiting for input
                    fresh_media = any(
                        f.is_file() and (is_audio_file(f) or is_zip_file(f) or is_image_file(f))
                        for f in current.rglob("*")
                    )
                    if fresh_media:
                        logging.warning(
                            "Media files appeared in '%s' while prompting — skipping deletion.", current.name
                        )
                        folder_seen_times.pop(current, None)
                    else:
                        logging.info("Deleting non-media folder: %s", current.name)
                        if not dry_run:
                            shutil.rmtree(current)
                except OSError as e:
                    logging.error("Failed to delete folder %s: %s", current.name, e)
            elif ans in {"i", "ignore"}:
                ignore_list.add(rel_key)
                save_ignore_list(root, ignore_list)
                logging.info("Added '%s' to ignore list.", rel_display)

            prompted_dirs.add(current)


def manage_ignorelist_interactive(root: Path) -> None:
    ignore_list = load_ignore_list(root)
    if not ignore_list:
        print("The ignore list is empty.")
        return
    items = sorted(ignore_list)
    print("\n=== Deletion Ignore List ===")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    raw = _input("\nEnter item numbers to remove (comma-separated), or press Enter to quit: ").strip()
    if not raw:
        return
    try:
        indices = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        print("Invalid input. No changes made.")
        return
    to_remove = {items[i - 1] for i in indices if 1 <= i <= len(items)}
    if to_remove:
        ignore_list -= to_remove
        save_ignore_list(root, ignore_list)
        print(f"Removed {len(to_remove)} item(s) from the ignore list.")
    else:
        print("No valid item numbers entered.")


def manage_album_ignorelist_interactive(root: Path) -> None:
    ignore_set = load_album_ignore_list(root)

    while True:
        items = sorted(ignore_set)
        print("\n=== Album Partial-Match Ignore List ===")
        if items:
            for i, (artist, album) in enumerate(items, 1):
                label = f"{artist}/{album}" if artist else album
                print(f"  {i}. {label}")
        else:
            print("  (empty)")

        print("\nOptions:")
        print("  r <numbers> = remove entries (e.g. 'r 1,3')")
        print("  a           = add a new entry manually")
        print("  Enter       = save and quit")

        raw = _input("\nChoice: ").strip()

        if not raw:
            break

        if raw.lower().startswith("r"):
            numbers_part = raw[1:].strip()
            if not numbers_part:
                print("Provide numbers after 'r', e.g. 'r 1,3'.")
                continue
            try:
                indices = {int(x.strip()) for x in numbers_part.split(",") if x.strip()}
            except ValueError:
                print("Invalid numbers.")
                continue
            to_remove = {items[i - 1] for i in indices if 1 <= i <= len(items)}
            if to_remove:
                ignore_set -= to_remove
                removed_labels = [f"{a}/{b}" if a else b for a, b in to_remove]
                print(f"Removed: {', '.join(removed_labels)}")
            else:
                print("No valid item numbers.")

        elif raw.lower() == "a":
            artist_in = _input("  Artist (leave blank for any): ").strip()
            album_in = _input("  Album name: ").strip()
            if not album_in:
                print("  Album name cannot be empty.")
                continue
            artist_san = sanitize_folder_name(artist_in) if artist_in else ""
            album_san = sanitize_folder_name(album_in)
            key = (artist_san, album_san)
            if key in ignore_set:
                label = f"{artist_san}/{album_san}" if artist_san else album_san
                print(f"  '{label}' is already in the ignore list.")
            else:
                ignore_set.add(key)
                label = f"{artist_san}/{album_san}" if artist_san else album_san
                print(f"  Added '{label}'.")

        else:
            print("Unknown command. Use 'r <numbers>', 'a', or Enter to quit.")

    save_album_ignore_list(root, ignore_set)
    print(f"Album ignore list saved ({len(ignore_set)} entries).")


def _update_multidisc_cache(tracks: Iterable[Path]) -> None:
    """Populate ALBUM_MULTI_DISC_CACHE from track metadata."""
    for path in tracks:
        meta = get_cached_metadata(path)
        album = meta.get("album")
        if not album:
            continue
        artist = sanitize_folder_name(meta.get("albumartist") or meta.get("artist") or "")
        key = (artist, sanitize_folder_name(album))
        if not ALBUM_MULTI_DISC_CACHE.get(key):
            d_num = (meta.get("disc_num") or "").strip()
            d_tot = (meta.get("disc_total") or "").strip()
            if (d_tot.isdigit() and int(d_tot) > 1) or (d_num.isdigit() and int(d_num) > 1):
                ALBUM_MULTI_DISC_CACHE[key] = True


def _print_safe_conflicts() -> None:
    """Print the safe-mode conflict summary (used by both --once and watch Ctrl+C exit)."""
    if not (SAFE_ARTIST_CONFLICTS or SAFE_ALBUM_CONFLICTS):
        return
    print(clr_yellow("\n[!] Safe-mode held back the following conflicts (not moved):"))
    if SAFE_ARTIST_CONFLICTS:
        print(clr_bold("\n  Artist conflicts (--no-partial-artist):"))
        for i, (a, b) in enumerate(sorted(SAFE_ARTIST_CONFLICTS), 1):
            print(f"    {i}. {clr_red(a)}  ~  {clr_green(b)}")
    if SAFE_ALBUM_CONFLICTS:
        print(clr_bold("\n  Album conflicts (--no-partial-album):"))
        for i, (artist, album_new, album_existing) in enumerate(sorted(SAFE_ALBUM_CONFLICTS), 1):
            artist_label = f"{clr_cyan(artist)} / " if artist else ""
            print(f"    {i}. {artist_label}{clr_green(album_new)}  ~  {clr_red(album_existing)}")


def run_once(
    root: Path,
    dry_run: bool,
    hierarchy: bool,
    skip_art: bool,
    skip_cleanup: bool,
    ignore_list: Optional[set] = None,
    cleanup_cooldown: float = DEFAULT_CLEANUP_COOLDOWN_SECONDS,
    no_convert: bool = False,
    no_partial_album: bool = False,
    no_partial_artist: bool = False,
    safe: bool = False,
) -> None:
    if ignore_list is None:
        ignore_list = set()
    ALBUM_IGNORE_ALL.update(load_album_ignore_list(root))
    logging.info("Running in one-shot optimization mode...")
    
    archives = [p for p in iter_watch_files(root) if is_zip_file(p)]
    if archives:
        logging.info("Found %d zip archive(s) to unpack.", len(archives))
        processed_archives: Dict[Path, FileStamp] = {}
        for archive_path in archives:
            try:
                process_archive(archive_path, root, dry_run, processed_archives, hierarchy)
            except Exception as exc:
                logging.error("Failed to process archive %s: %s", archive_path, exc)

    audio_files = [p for p in iter_watch_files(root) if is_audio_file(p)]
    approved_conversions: set[str] = set()
    approved_art: set[str] = set()
    
    if audio_files:
        logging.info("Pre-scanning metadata to reliably identify multi-disc albums...")
        _update_multidisc_cache(audio_files)

        logging.info("Analyzing metadata and organizing %d track(s)...", len(audio_files))
        for path in audio_files:
            try:
                meta = get_cached_metadata(path)
                album = meta["album"]
                if not album:
                    logging.debug("Skipping: No album metadata found for %s", path)
                    continue

                meta = ensure_albumartist(path, meta)
                try:
                    destination_dir, target_stem, is_orphan = get_destination_info(
                        root, path, meta, hierarchy, no_partial_album, no_partial_artist, safe
                    )
                except SafeAbortError as exc:
                    logging.warning("Safe-abort: held back '%s' — %s", path.name, exc)
                    continue

                if not within_root(destination_dir, root):
                    logging.warning("Skipping unsafe destination path for album %r", album)
                    continue

                final_mp3 = None
                if path.suffix.lower() == MP3_SUFFIX:
                    if path.parent.resolve() != destination_dir.resolve() or path.stem != target_stem:
                        if not skip_art:
                            move_associated_images(path.parent, destination_dir, dry_run)
                        final_mp3 = move_file(path, destination_dir, target_stem, root, dry_run)
                    else:
                        final_mp3 = path
                else:
                    if no_convert:
                        logging.debug("Skipping non-MP3 file (--no-convert): %s", path.name)
                        continue
                    if prompt_for_conversion(album, path, approved_conversions):
                        if not skip_art:
                            move_associated_images(path.parent, destination_dir, dry_run)
                        final_mp3 = convert_to_mp3(path, destination_dir, target_stem, root, dry_run, meta)
                    else:
                        logging.info("Skipped conversion for %s", path.name)

                if is_orphan and final_mp3:
                    logging.warning("Track '%s' has been orphaned at: %s", final_mp3.name, destination_dir)

                if final_mp3 and final_mp3.exists() and not skip_art:
                    if not has_album_art(final_mp3):
                        dest_images = [p for p in destination_dir.iterdir() if is_image_file(p)]
                        if dest_images:
                            img_path = dest_images[0]
                            if prompt_for_art(album, final_mp3, img_path, approved_art):
                                if not dry_run:
                                    apply_external_art(final_mp3, img_path)

            except Exception as exc:
                logging.error("Failed to process track %s: %s", path, exc)
    else:
        logging.info("No audio tracks found to organize.")

    if not skip_art:
        images = [p for p in root.rglob("*") if is_image_file(p)]
        for img in images:
            try:
                mp3_peers = [p for p in img.parent.iterdir() if p.suffix.lower() == MP3_SUFFIX]
                for mp3 in mp3_peers:
                    if not has_album_art(mp3):
                        meta = get_cached_metadata(mp3)
                        album_name = meta["album"] or img.parent.name
                        if prompt_for_art(album_name, mp3, img, approved_art):
                            if not dry_run: apply_external_art(mp3, img)
            except OSError:
                pass

    if not skip_cleanup:
        cleanup_stale_folders(root, set(), dry_run, ignore_list, cooldown=cleanup_cooldown)

    logging.info("One-shot organization sequence completed.")

    if safe:
        _print_safe_conflicts()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)

    try:
        root = normalize_root(args.root)
    except Exception as exc:
        logging.error(str(exc))
        return 1

    global _DRY_RUN, _REPROCESS_ORPHANS
    _DRY_RUN = args.dry_run
    _REPROCESS_ORPHANS = args.reprocess_orphans
    if _DRY_RUN:
        logging.info("DRY-RUN mode: no files will be moved, created, or modified.")
    if _REPROCESS_ORPHANS:
        logging.info("Orphan folders will be re-scanned (--reprocess-orphans).")

    if args.manage_ignorelist:
        manage_ignorelist_interactive(root)
        return 0

    if args.manage_album_ignorelist:
        manage_album_ignorelist_interactive(root)
        return 0

    ignore_list = load_ignore_list(root) if not args.no_folder_ignorelist else set()
    if not args.no_album_ignorelist:
        ALBUM_IGNORE_ALL.update(load_album_ignore_list(root))

    if args.once:
        run_once(
            root, args.dry_run, args.hierarchy, args.skip_art, args.skip_cleanup,
            ignore_list, args.cleanup_cooldown,
            no_convert=args.no_convert,
            no_partial_album=args.no_partial_album,
            no_partial_artist=args.no_partial_artist,
            safe=args.safe,
        )
        return 0

    logging.info("Watching %s", root)
    if args.hierarchy:
        logging.info("Sorting Hierarchy: Root / Main Artist / Album / [Disc] / 01 Title")
    if not args.skip_art:
        logging.info("Automatic Album Art detection Enabled")
    if not args.skip_cleanup:
        logging.info("Automatic Directory Cleanup Enabled")

    logging.info("Scanning every %.2f seconds", args.interval)

    previous_snapshot = build_snapshot(root)
    pending_items: Dict[Path, PendingEntry] = {}
    processed_archives: Dict[Path, FileStamp] = {}

    approved_conversions: set[str] = set()
    approved_art: set[str] = set()
    prompted_dirs: set[Path] = set()
    folder_seen_times: Dict[Path, float] = {}

    if not args.skip_cleanup:
        logging.info("Performing initial directory cleanup sweep...")
        cleanup_stale_folders(root, prompted_dirs, args.dry_run, ignore_list, args.cleanup_cooldown, folder_seen_times)
    
    last_cleanup_time = time.monotonic()

    try:
        while True:
            try:
                current_snapshot = build_snapshot(root)
            except Exception as exc:
                logging.error("Scan failed: %s", exc)
                time.sleep(args.interval)
                continue

            added, removed, modified = scan_for_changes(previous_snapshot, current_snapshot)
            now = time.monotonic()

            for path in added + modified:
                if is_zip_file(path):
                    processed_archives.pop(path, None)
                elif is_audio_file(path) or is_image_file(path):
                    old = pending_items.get(path)
                    pending_items[path] = PendingEntry(
                        first_seen=now,
                        first_ever_seen=old.first_ever_seen if old else now,
                        stamp=current_snapshot[path],
                        asked_conversion=old.asked_conversion if old else False,
                    )

            for path in removed:
                pending_items.pop(path, None)
                processed_archives.pop(path, None)

            for path in list(pending_items.keys()):
                if path not in current_snapshot:
                    pending_items.pop(path, None)
                    continue
                current_stamp = current_snapshot[path]
                entry = pending_items[path]
                if entry.stamp != current_stamp:
                    if (now - entry.first_ever_seen) >= _MAX_SETTLE_SECONDS:
                        # Hard timeout: stamp keeps changing but file has waited long enough.
                        # Preserve first_seen so it passes the settle check next cycle.
                        logging.warning(
                            "'%s' has been unstable for %.0fs — will process next cycle.",
                            path.name, now - entry.first_ever_seen,
                        )
                        pending_items[path] = PendingEntry(
                            first_seen=entry.first_seen,
                            first_ever_seen=entry.first_ever_seen,
                            stamp=current_stamp,
                            asked_conversion=entry.asked_conversion,
                        )
                    else:
                        pending_items[path] = PendingEntry(
                            first_seen=now,
                            first_ever_seen=entry.first_ever_seen,
                            stamp=current_stamp,
                            asked_conversion=entry.asked_conversion,
                        )

            if added or removed or modified:
                for path in added:
                    logging.info("Added: %s", path)
                for path in modified:
                    logging.info("Modified: %s", path)
                for path in removed:
                    logging.info("Removed: %s", path)

            for archive_path in list(current_snapshot.keys()):
                if not is_zip_file(archive_path):
                    continue
                try:
                    stat_result = archive_path.stat()
                except OSError:
                    continue
                stamp = FileStamp(size=stat_result.st_size, mtime_ns=stat_result.st_mtime_ns)
                if processed_archives.get(archive_path) == stamp:
                    continue

                archive_pending = pending_items.get(archive_path)
                if archive_pending is None:
                    pending_items[archive_path] = PendingEntry(
                        first_seen=now, first_ever_seen=now, stamp=stamp
                    )
                    continue
                if archive_pending.stamp != stamp:
                    pending_items[archive_path] = PendingEntry(
                        first_seen=now,
                        first_ever_seen=archive_pending.first_ever_seen,
                        stamp=stamp,
                        asked_conversion=archive_pending.asked_conversion,
                    )
                    continue
                if (now - archive_pending.first_seen) < args.settle:
                    continue

                try:
                    process_archive(archive_path, root, args.dry_run, processed_archives, args.hierarchy)
                except Exception as exc:
                    logging.error("Failed to process archive %s: %s", archive_path, exc)

            ready_tracks = [
                path for path, entry in pending_items.items()
                if is_audio_file(path) and path in current_snapshot and (now - entry.first_seen) >= args.settle
            ]
            ready_images = [
                path for path, entry in pending_items.items()
                if is_image_file(path) and path in current_snapshot and (now - entry.first_seen) >= args.settle
            ]

            if ready_tracks:
                _update_multidisc_cache(ready_tracks)

            for track_path in ready_tracks:
                try:
                    process_track(
                        track_path,
                        root,
                        args.dry_run,
                        pending_items,
                        approved_conversions,
                        approved_art,
                        args.hierarchy,
                        args.skip_art,
                        no_convert=args.no_convert,
                        no_partial_album=args.no_partial_album,
                        no_partial_artist=args.no_partial_artist,
                        safe=args.safe,
                    )
                except Exception as exc:
                    logging.error("Failed to process track %s: %s", track_path, exc)

            for img_path in ready_images:
                try:
                    process_image(img_path, root, args.dry_run, pending_items, approved_art, args.skip_art, args.hierarchy)
                except Exception as exc:
                    logging.error("Failed to process image %s: %s", img_path, exc)

            if not args.skip_cleanup and (now - last_cleanup_time) >= CLEANUP_INTERVAL_SECONDS:
                cleanup_stale_folders(root, prompted_dirs, args.dry_run, ignore_list, args.cleanup_cooldown, folder_seen_times)
                last_cleanup_time = now

            previous_snapshot = current_snapshot
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logging.info("Watcher stopped.")
        if args.safe:
            _print_safe_conflicts()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
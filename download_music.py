#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-source Hi-Res Music Downloader  (PARALLEL MODE)
======================================================
ALL songs + lyrics are downloaded in parallel (ThreadPoolExecutor).

Priority Sources (highest quality wins, OST priority + FLAC — no non-OST lossy):
  1. Sockseek (fiso64/sockseek) - Soulseek P2P, highest priority
  2. Internet Archive (archive.org) - Public domain FLAC
  3. Free Music Archive (freemusicarchive.org) - Free FLAC
  4. Jamendo (jamendo.com) - Free lossless
  5. YouTube via yt-dlp - Best audio -> FLAC conversion
  6. Bandcamp via yt-dlp - FLAC when available (free releases)
  7. NetEase Cloud Music (网易云音乐) - FLAC download
  8. QQ Music (QQ音乐) - FLAC download
  9. Kugou Music (酷狗音乐) - FLAC download
  10. Migu Music (咪咕音乐) - Hi-Res FLAC download

Supported Playlist Platforms (auto-detected by URL):
  - QQ Music       (y.qq.com, c6.y.qq.com short-links)
  - NetEase Cloud  (music.163.com)
  - Kugou Music    (kugou.com)
  - Kuwo Music     (kuwo.cn)
  - 汽水音乐       (music.douyin.com / qishui)
  - 咪咕音乐       (music.migu.cn)
  - Spotify        (open.spotify.com) — via spotdl/ytdlp search
  - YouTube Music  (music.youtube.com)
  - Apple Music    (music.apple.com)

Parallelism design:
  - Songs: all downloaded concurrently (--workers, default 8)
  - Each song: lyrics fetched in parallel with audio source search
  - Sockseek: serialized via semaphore (1 at a time) to avoid
    Soulseek login conflicts; other 9 sources fully concurrent
  - Report JSON written with a lock (thread-safe)
  - Progress bar: thread-safe per-song status display

Quality target: 24-bit / 192kHz FLAC (or best available)
Format policy: FLAC only — MP3/AAC/OGG are rejected at source level
Resume: download_report.json tracks state per song
Encoding: UTF-8 throughout, no garbled characters
"""

import os
import re
import sys
import json
import time
import shutil
import socket
import struct
import random
import logging
import argparse
import threading
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# ── Force UTF-8 output on Windows (prevents garbled text) ────────────────────
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")

# ── China network optimization: increase urllib default timeouts & retries ────
# Standard library HTTP handler: retry up to 3 times, 30 s connect timeout
import urllib.error
_default_opener = urllib.request.build_opener(
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
)
urllib.request.install_opener(_default_opener)

# Patch socket default timeout so all urllib calls respect a global floor
import socket as _socket_mod
_socket_mod.setdefaulttimeout(30)

# Enable HTTP keep-alive via a connection-pooling opener (avoids TCP handshake
# overhead on repeated requests to the same host — important for CN latency)
try:
    import http.client as _http_client
    _http_client.HTTPConnection.debuglevel = 0
except Exception:
    pass

# ── Enable ANSI color codes on Windows 10+ CMD ───────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore
        # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) on stdout handle
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong(0)
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

_ANSI_GREEN  = "\033[92m"
_ANSI_YELLOW = "\033[93m"
_ANSI_CYAN   = "\033[96m"
_ANSI_DIM    = "\033[2m"
_ANSI_BOLD   = "\033[1m"
_ANSI_RESET  = "\033[0m"


def _green(text: str) -> str:
    """Wrap text in ANSI green color codes."""
    return f"{_ANSI_GREEN}{text}{_ANSI_RESET}"


def _arrow_select_or_input(
    header_lines: list,
    default_text: str,
    default_hint: str,
    input_hints: list,
    box_width: int = 56,
    input_lines: int = 3,
):
    r"""
    Interactive arrow-key selection: 'use default' vs 'input custom'.
    Uses msvcrt on Windows for non-blocking key reading.
    Falls back to numbered menu on non-Windows.

    Layout (focus=0, default selected):
      ► 使用默认下载目录D:\MyMusic  按Enter键确认  (green+bold, hint inline on same line)
      (blank line)
      输入自定义下载目录...       (cyan, always visible)
      不存在的目录将自动创建      (cyan, always visible)
      ┌──────────────┐           (dim cyan border when unfocused)
      │              │           (empty — 3 lines)
      │              │
      │              │
      └──────────────┘
      ↑↓ 移动焦点，Enter 确认    (cyan)

    Layout (focus=1, input selected):
        使用默认下载目录D:\MyMusic  (normal color, not dimmed)
      (blank line)
      输入自定义下载目录...       (cyan, always visible)
      不存在的目录将自动创建      (cyan, always visible)
      ┌──────────────┐           (yellow+bold border when focused)
      │ C:\\Music█                │ (user input + cursor)
      │                          │
      │                          │
      └──────────────┘
      ↑↓ 移动焦点，输入后按 Enter 确认，ESC 清空  (cyan)

    Returns: ("default", None) or ("input", user_text)
    """
    try:
        import msvcrt
        _has_msvcrt = sys.stdin.isatty() and sys.stdout.isatty()
    except (ImportError, AttributeError):
        _has_msvcrt = False

    if not _has_msvcrt:
        # Fallback: numbered menu
        for line in header_lines:
            print(line)
        print()
        print(f"  [1] {default_text}")
        print(f"  [2] 输入自定义")
        sys.stdout.flush()
        try:
            sel = input("  请选择 [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            sel = "1"
        if sel in ("", "1"):
            return ("default", None)
        try:
            user_input = input("  请输入: ").strip()
        except (EOFError, KeyboardInterrupt):
            user_input = ""
        return ("input", user_input) if user_input else ("default", None)

    focus = 0          # 0 = default, 1 = input
    input_buf = ""

    # Print header (printed once, not redrawn)
    for line in header_lines:
        print(line)
    sys.stdout.flush()

    # Strip ANSI codes to measure visible character width
    import re
    _ANSI_RE = re.compile(r'\033\[[0-9;]*m')

    def _vis_len(s: str) -> int:
        """Return the visible (non-ANSI) character length of a string."""
        return len(_ANSI_RE.sub('', s))

    def _build_lines():
        """Build the dynamic area lines based on current state."""
        lines = []

        # --- Default option line (hint inline on same line when focused, not dimmed when unfocused) ---
        if focus == 0:
            lines.append(
                f"  {_ANSI_GREEN}{_ANSI_BOLD}► {default_text}"
                f"{_ANSI_GREEN}{_ANSI_BOLD}  {default_hint}{_ANSI_RESET}"
            )
        else:
            lines.append(f"  {_ANSI_CYAN}{default_text}{_ANSI_RESET}")

        # --- Blank separator between default option and input hints ---
        lines.append("")

        # --- Input hint lines (always visible, outside the box, cyan color) ---
        for hint in input_hints:
            lines.append(f"  {_ANSI_CYAN}{hint}{_ANSI_RESET}")

        # --- Input box ---
        _bc = (_ANSI_YELLOW + _ANSI_BOLD) if focus == 1 else (_ANSI_DIM + _ANSI_CYAN)

        lines.append(f"  {_bc}┌{'─' * box_width}┐{_ANSI_RESET}")

        # Line 1: user input + cursor (or empty when unfocused)
        if focus == 1:
            display = input_buf
            max_display = box_width - 3  # 2 spaces left pad + 1 for cursor
            if len(display) > max_display:
                display = "..." + display[-(max_display - 3):]
            # Calculate padding: box_width = 2(left pad) + display_len + 1(cursor) + padding
            _pad = box_width - 2 - len(display) - 1
            lines.append(
                f"  {_bc}│{_ANSI_RESET}  {display}{_ANSI_YELLOW}█{_ANSI_RESET}"
                + f"{' ' * _pad}{_bc}│{_ANSI_RESET}"
            )
        else:
            # Unfocused: empty line inside box (2 spaces left pad + spaces + right border)
            lines.append(f"  {_bc}│{_ANSI_RESET}{' ' * box_width}{_bc}│{_ANSI_RESET}")

        # Remaining lines inside box: empty padding
        for _ in range(1, input_lines):
            lines.append(f"  {_bc}│{_ANSI_RESET}{' ' * box_width}{_bc}│{_ANSI_RESET}")

        lines.append(f"  {_bc}└{'─' * box_width}┘{_ANSI_RESET}")

        # --- Navigation hint (cyan, no dim) ---
        if focus == 0:
            lines.append(f"  {_ANSI_CYAN}↑↓ 移动焦点，Enter 确认{_ANSI_RESET}")
        else:
            lines.append(f"  {_ANSI_CYAN}↑↓ 移动焦点，输入后按 Enter 确认，ESC 清空{_ANSI_RESET}")

        return lines

    # First render
    dyn = _build_lines()
    DYN_COUNT = len(dyn)
    for line in dyn:
        print(line)
    sys.stdout.flush()

    def _redraw():
        """Move cursor up and redraw the dynamic area in place."""
        sys.stdout.write(f"\033[{DYN_COUNT}A")
        new_dyn = _build_lines()
        for line in new_dyn:
            sys.stdout.write(f"\r\033[K{line}\n")
        sys.stdout.flush()

    # Key handling loop
    while True:
        ch = msvcrt.getch()

        if ch in (b'\x00', b'\xe0'):
            # Special key prefix (arrow keys, etc.)
            ch2 = msvcrt.getch()
            if ch2 == b'H':  # Up arrow
                if focus > 0:
                    focus = 0
                    _redraw()
            elif ch2 == b'P':  # Down arrow
                if focus < 1:
                    focus = 1
                    _redraw()
        elif ch == b'\r':  # Enter
            if focus == 0:
                print()
                return ("default", None)
            elif focus == 1:
                if input_buf.strip():
                    print()
                    return ("input", input_buf.strip())
                # Empty input — ignore
        elif ch == b'\x03':  # Ctrl+C
            raise KeyboardInterrupt
        elif focus == 1:
            # Input mode: handle text entry
            if ch == b'\x08':  # Backspace
                if input_buf:
                    input_buf = input_buf[:-1]
                    _redraw()
            elif ch == b'\x1b':  # ESC — clear input
                if input_buf:
                    input_buf = ""
                    _redraw()
            elif 32 <= ch[0] <= 126:  # Printable ASCII
                input_buf += chr(ch[0])
                _redraw()

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

# ── Global config (filled by main()) ─────────────────────────────────────────
PROXY:             Optional[str] = None
SOCKSEEK_EXE:      str = ""
SOCKSEEK_USER:     str = "zpf10284140"
SOCKSEEK_PASS:     str = "zpf123,"
SOCKSEEK_MINBR:    int = 3000
SOCKSEEK_TIMEOUT:  int = 180
SOCKSEEK_CONF:     str = ""
WORKERS:           int = 8   # parallel song workers

# ── China network acceleration config ────────────────────────────────────────
# Enabled by --cn-accelerate flag (or auto-detected via latency probe)
CN_ACCELERATE:     bool = False

# aria2c executable (auto-detected from PATH or bundled)
ARIA2C_EXE:        str = ""

# ffmpeg executable (for converting downloaded audio to FLAC)
FFMPEG_EXE:        str = ""

# Number of aria2c parallel connections per file (国内多线程分片，大幅提速)
ARIA2C_CONNECTIONS: int = 16

# GitHub acceleration mirrors (used for auto-downloading sockseek.exe from CN)
# These are free public proxies operated by Chinese developers
_GITHUB_CN_MIRRORS = [
    "https://ghproxy.cn/",          # ghproxy.cn — high-speed CN mirror
    "https://gh-proxy.com/",         # gh-proxy.com
    "https://ghfast.top/",           # ghfast.top
    "https://github.moeyy.xyz/",     # moeyy mirror
    "https://mirror.ghproxy.com/",   # mirror.ghproxy
]

# CDN acceleration node mapping for overseas hosts (免费国内公共加速节点)
# Internet Archive has a partial mirror via CERNET2 / 教育网节点
# For general HTTPS acceleration we route through open CN CDN proxies
_CN_CDN_ACCELERATE_RULES: list = [
    # Internet Archive: use the official global CDN edge (Singapore/HK PoP)
    # — archive.org resolves to 207.241.224.x in US; HK CDN is faster from CN
    ("archive.org",      "https://archive.org"),          # direct (HK PoP faster)
    # Cloudflare-backed hosts (already have HK/SG PoPs, no rewrite needed)
    ("jamendo.com",      None),
    ("freemusicarchive.org", None),
]

# ── Thread-safety primitives ──────────────────────────────────────────────────
# Sockseek: only 1 concurrent process to avoid Soulseek login conflicts
_sockseek_sem   = threading.Semaphore(1)
_sockseek_offline = False   # Global flag: if Soulseek server is unreachable,
                             # skip subsequent Sockseek attempts temporarily
_sockseek_offline_since = 0.0  # epoch timestamp when offline was detected
SOCKSEEK_OFFLINE_COOLDOWN = 120  # seconds before retrying after offline detection

# Soulseek server TCP probe cache (avoid probing every single song)
_sockseek_probe_last_ts = 0.0     # last probe timestamp
_sockseek_probe_result  = True    # last probe result (True = server reachable)
SOCKSEEK_PROBE_INTERVAL = 60      # seconds between probes when server is up


def _probe_soulseek_server() -> bool:
    """
    Quick TCP probe to server.slsknet.org:2242.

    The Soulseek server should accept TCP connections and keep them open
    while waiting for the client's login packet. If the server closes the
    connection immediately (recv returns 0 bytes), it is rejecting logins
    — likely due to IP ban, rate-limiting, or maintenance.

    Returns True if the server appears reachable, False otherwise.
    Results are cached for SOCKSEEK_PROBE_INTERVAL seconds.
    """
    global _sockseek_probe_last_ts, _sockseek_probe_result

    # Use cached result if probed recently
    elapsed = time.time() - _sockseek_probe_last_ts
    if elapsed < SOCKSEEK_PROBE_INTERVAL:
        return _sockseek_probe_result

    _sockseek_probe_last_ts = time.time()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8)  # 8s connect timeout
        t0 = time.time()
        s.connect(("server.slsknet.org", 2242))
        connect_ms = (time.time() - t0) * 1000

        # Check if server keeps connection open or closes it immediately
        s.settimeout(3)
        try:
            data = s.recv(4)
            if len(data) == 0:
                # Server closed connection — not accepting logins
                log.info(f"  [SOCKSEEK] Server probe: TCP connected ({connect_ms:.0f}ms) "
                         f"but server closed connection — offline")
                _sockseek_probe_result = False
            else:
                # Server sent data — online
                _sockseek_probe_result = True
        except socket.timeout:
            # No data but connection still open — server waiting for login
            _sockseek_probe_result = True
        s.close()
    except Exception as e:
        log.info(f"  [SOCKSEEK] Server probe: connection failed — {e}")
        _sockseek_probe_result = False

    return _sockseek_probe_result


def _get_free_port() -> int:
    """
    Pick a random available TCP port in the ephemeral range 40000-59999.

    Strategy (solves multi-CMD concurrency):
    1. Start from a random offset so different CMD windows start probing
       different port ranges and rarely collide.
    2. Each candidate is tested with SO_REUSEADDR=False so we get a
       reliable answer about whether the port is truly free right now.
    3. Falls back to OS-assigned port (bind 0) if all candidates fail.

    Note: there is always a small TOCTOU window between our test and
    sockseek binding the port.  The random start makes simultaneous
    collision probability negligible (1/20000 per pair of processes).
    """
    base = random.randint(40000, 59900)
    for offset in range(100):          # try up to 100 consecutive ports
        port = base + offset
        if port > 59999:
            port = 40000 + (port - 60000)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    # Ultimate fallback: let OS pick any free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]
# Report file write lock
_report_lock    = threading.Lock()
# Progress print lock (prevent interleaved output lines)
_print_lock     = threading.Lock()
# Counters shared across threads
_counter_lock   = threading.Lock()
_done_count     = 0   # songs finished (success + fail)

# ── Constants ─────────────────────────────────────────────────────────────────
LOG_FILE    = Path("download_log.txt")
REPORT_FILE = Path("download_report.json")
CHUNK_SIZE  = 1024 * 1024  # 1 MB

LRCLIB_SEARCH  = "https://lrclib.net/api/search?q={query}"
LRCLIB_GET     = "https://lrclib.net/api/get?artist_name={artist}&track_name={title}"
NETEASE_SEARCH = "https://music.163.com/api/search/get/web?csrf_token=&s={query}&type=1&offset=0&total=true&limit=3"
NETEASE_LYRIC  = "https://music.163.com/api/song/lyric?id={id}&lv=1&tv=-1"
QQMUSIC_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&inCharset=utf8&outCharset=utf-8&key={query}&num=3&t=0"
QQMUSIC_LYRIC  = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcgi?songmid={mid}&format=json&inCharset=utf8&outCharset=utf-8"
MUSIXMATCH_SEARCH = "https://apic-desktop.musixmatch.com/ws/1.1/track.search?q_track={title}&q_artist={artist}&page_size=3&page=1&s_track_rating=desc&usertoken=190523f77464fba06fa5f82a9bfbd5fb5f8fdbc4d8be9a13&app_id=web-desktop-app-v1.0"
MEGALOBIZ_SEARCH  = "https://www.megalobiz.com/search/all?q={query}"
ARCHIVE_SEARCH = (
    "https://archive.org/advancedsearch.php"
    "?q={query}+mediatype:audio+format:FLAC"
    "&fl[]=identifier,title,creator&rows=5&output=json"
)
FMA_SEARCH = "https://freemusicarchive.org/api/get/tracks.json?title={title}&limit=3&api_key=60BLHNQCAOUFPIBZ"
JAMENDO_SEARCH = (
    "https://api.jamendo.com/v3.0/tracks/"
    "?client_id=b6747d04&format=json"
    "&name={title}&artist_name={artist}"
    "&audioformat=flac&limit=3"
)

SAFE_RE    = re.compile(r'[\\/:*?"<>|]')
AUDIO_EXTS = (".flac", ".wav", ".alac", ".aif", ".aiff", ".dsf", ".dsd",
              ".wv", ".ape", ".mp3", ".m4a", ".ogg", ".opus", ".aac")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".3gp",
              ".webm", ".m4v", ".mpg", ".mpeg", ".vob")

# Quality score constants  (higher = better)
QUALITY_HIRESFLAC = 100   # 24-bit FLAC >= 192kHz
QUALITY_FLAC96    = 90    # 24-bit FLAC  88.2 / 96 kHz
QUALITY_FLAC48    = 80    # 24-bit FLAC  44.1 / 48 kHz
QUALITY_FLAC16    = 70    # 16-bit FLAC
QUALITY_WAV       = 65    # WAV (unknown depth)
QUALITY_LOSSLESS  = 60    # other lossless (ape/wavpack/alac)

# OST (Original Soundtrack) bonus: OST versions get +5 priority
# OST keywords detected in title, album, or file name
OST_KEYWORDS_EN = ("ost", "original soundtrack", "soundtrack", "bgm",
                   "motion picture", "film score", "movie score")
OST_KEYWORDS_CN = ("原声", "原声带", "原声大碟", "原声音乐", "配乐",
                   "电影原声", "动漫原声", "游戏原声", "影视原声",
                   "ost", "bgm", "插曲")
OST_BONUS       = 30      # large bonus: OST files preferred over non-OST FLAC
QUALITY_MP3_320   = 40    # MP3 320 kbps
QUALITY_MP3_HIGH  = 30    # MP3 >= 192 kbps
QUALITY_AAC_HIGH  = 25
QUALITY_LOSSY     = 10    # anything else
QUALITY_UNKNOWN   = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Make StreamHandler thread-safe: wrap emit with the print lock
class _LockedStreamHandler(logging.StreamHandler):
    def emit(self, record):
        with _print_lock:
            super().emit(record)

for _h in log.root.handlers:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
        _h.__class__ = _LockedStreamHandler


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def safe_name(name: str) -> str:
    result = SAFE_RE.sub("_", name).strip()
    # Remove trailing dots/spaces (Windows path issue)
    result = result.rstrip(". ")
    return result[:200]


def _line_to_song(line: str) -> Optional[dict]:
    """Parse a single 'Title - Artist' line into a song dict."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(" - ", 1)
    if len(parts) == 2:
        title  = parts[0].strip()
        artist = parts[1].strip()
    else:
        title  = line
        artist = "Unknown"
    primary_artist = re.split(r"[/、,，]", artist)[0].strip()
    return {
        "title":          title,
        "artist":         artist,
        "primary_artist": primary_artist,
        "raw":            line,
    }


def parse_playlist(path: str) -> list:
    songs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            song = _line_to_song(line)
            if song:
                songs.append(song)
    return songs


# ─────────────────────────────────────────────────────────────────────────────
# Universal Playlist / Track URL Parser
#
# Engine priority (by GitHub stars / reliability):
#   1. yt-dlp          ★80k+  handles YouTube/YTMusic/Spotify(via plugin)/
#                              Soundcloud/Bandcamp/etc.  No API key needed.
#   2. QQ Music        internal API — 3 fallback endpoints
#   3. NetEase Cloud   internal API — 2 fallback endpoints + trackId resolve
#   4. Kugou Music     internal API + HTML scrape fallback
#   5. Kuwo Music      internal API
#   6. 汽水/Douyin     internal API + HTML scrape
#   7. 咪咕音乐        internal API
#   8. Apple Music     JSON-LD scrape (no API key needed)
#
# All HTTP requests route through the system proxy (PROXY global).
# China-optimized: keep-alive sessions, retries, 25 s base timeout.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PLAYLIST_URL = "https://c6.y.qq.com/base/fcgi-bin/u?__=RI5L3W4QHae1"

# Rotating UA pool — reduces bot-detection false-positives
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]
_ua_idx = 0

def _next_ua() -> str:
    global _ua_idx
    ua = _UA_POOL[_ua_idx % len(_UA_POOL)]
    _ua_idx += 1
    return ua


def _auto_detect_proxy() -> Optional[str]:
    """
    Auto-detect system proxy from environment variables and Windows registry.

    Checks (in order):
      1. HTTP_PROXY / HTTPS_PROXY environment variables
      2. Windows registry: HKCU\\...\\Internet Settings\\ProxyEnable + ProxyServer
      3. urllib.request.getproxies() (cross-platform fallback)

    Returns proxy URL string (e.g. 'http://localhost:21879') or None.
    """
    # 1. Environment variables (most reliable on all platforms)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        val = os.environ.get(var)
        if val:
            return val

    # 2. Windows registry (IE / system proxy settings)
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            try:
                proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if proxy_enable:
                    proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    # ProxyServer can be "host:port" or "http=host:port;https=host:port"
                    if "=" in proxy_server:
                        for part in proxy_server.split(";"):
                            if part.startswith("http="):
                                val = part[5:]
                                return val if val.startswith("http") else "http://" + val
                    elif proxy_server:
                        return ("http://" + proxy_server
                                if not proxy_server.startswith("http")
                                else proxy_server)
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass

    # 3. Cross-platform fallback (uses env vars + registry on Windows)
    try:
        proxies = urllib.request.getproxies()
        if proxies.get("http"):
            return proxies["http"]
        if proxies.get("https"):
            return proxies["https"]
    except Exception:
        pass

    return None


# ── Proxy environment variable names (both cases for cross-tool compat) ──────
_PROXY_ENV_VARS = [
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
]
_NO_PROXY_HOSTS = "localhost,127.0.0.1,::1,0.0.0.0"


def _setup_proxy(explicit_proxy: Optional[str] = None) -> Optional[str]:
    """
    Auto-detect system proxy and configure it for ALL network connections.

    When a proxy is detected (or explicitly provided):
      1. Sets http_proxy / https_proxy / ALL_PROXY env vars (both cases)
         so that ALL subprocesses (yt-dlp, aria2c, sockseek/.NET, curl, etc.)
         inherit the proxy automatically.
      2. Sets NO_PROXY for localhost to avoid proxying local connections.
      3. Installs a proxy-aware urllib opener so urllib.request.urlopen()
         fallback paths also route through the proxy.
      4. Resets the cached requests.Session so it picks up the new proxy.

    When no proxy is detected:
      Clears ALL proxy env vars to ensure direct (no-proxy) connections,
      even if the system had proxy env vars set externally.

    Returns the proxy URL string or None.
    """
    global PROXY, _session

    # Determine proxy: explicit > auto-detect
    proxy = explicit_proxy or _auto_detect_proxy()

    # Clear ALL existing proxy env vars first (clean slate)
    for var in _PROXY_ENV_VARS:
        os.environ.pop(var, None)
    os.environ.pop("no_proxy", None)
    os.environ.pop("NO_PROXY", None)

    if proxy:
        # ── Proxy ON: set env vars for all tools ──
        # Ensure it has a scheme (default http://)
        if not proxy.startswith(("http://", "https://", "socks5://", "socks4://")):
            proxy = "http://" + proxy

        for var in _PROXY_ENV_VARS:
            os.environ[var] = proxy

        # Don't proxy localhost connections
        os.environ["no_proxy"] = _NO_PROXY_HOSTS
        os.environ["NO_PROXY"] = _NO_PROXY_HOSTS

        # ── Install proxy-aware urllib opener ──
        # urllib.request.urlopen() respects env vars, but the default opener
        # may be cached. Install an explicit opener to be safe.
        try:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy,
                "https": proxy,
            })
            opener = urllib.request.build_opener(proxy_handler)
            urllib.request.install_opener(opener)
        except Exception:
            pass  # env vars will still be picked up by default handler

        log.info(f"[PROXY] System proxy detected and enabled: {proxy}")
        log.info(f"[PROXY]   → Effective for: requests, urllib, yt-dlp, aria2c, sockseek, all subprocesses")
    else:
        # ── Proxy OFF: ensure NO proxy is used ──
        # Install a direct (no-proxy) urllib opener
        try:
            proxy_handler = urllib.request.ProxyHandler({})  # empty = no proxy
            opener = urllib.request.build_opener(proxy_handler)
            urllib.request.install_opener(opener)
        except Exception:
            pass

        log.info("[PROXY] No system proxy detected — using direct connection")

    # Update global and reset cached session
    PROXY = proxy
    _session = None  # force _get_session() to rebuild with new proxy

    return proxy


# ── requests Session (connection-pool / keep-alive) ───────────────────────────
# One session per process — reuses TCP connections across playlist API calls.
_session = None

def _get_session():
    """Return a requests.Session configured with proxy + keep-alive + retries."""
    global _session
    if _session is not None:
        return _session
    if not HAS_REQUESTS:
        return None
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter
    s = _requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "POST"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=16,
        pool_maxsize=32,
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": _BROWSER_UA})
    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    _session = s
    return s


def _resolve_short_url(url: str) -> str:
    """Follow HTTP redirects to get the final URL (no content download).

    Uses GET with stream=True because some servers (e.g. c6.y.qq.com)
    return HTTP 500 for HEAD requests but work fine with GET.
    """
    try:
        sess = _get_session()
        if sess:
            r = sess.get(url, allow_redirects=True, timeout=15, stream=True,
                         headers={"User-Agent": _BROWSER_UA})
            final_url = r.url
            r.close()
            return final_url
        else:
            req = urllib.request.Request(
                url, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.url
    except Exception:
        return url


def _http_get_text(url: str, headers: dict = None, timeout: int = 25,
                   retries: int = 4) -> Optional[str]:
    """
    GET request returning response text, or None on failure.
    Uses keep-alive Session + exponential retry for CN network.
    """
    h = {"User-Agent": _next_ua()}
    if headers:
        h.update(headers)
    last_exc = None
    sess = _get_session()
    for attempt in range(1, retries + 1):
        try:
            if sess:
                r = sess.get(url, headers=h, timeout=timeout)
                r.raise_for_status()
                return r.text
            else:
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = min(2 ** (attempt - 1), 8)
                time.sleep(wait)
    log.debug(f"[PLAYLIST] GET failed {url}: {last_exc}")
    return None


def _http_get_json_h(url: str, headers: dict = None, timeout: int = 25):
    """GET request returning parsed JSON, or None on failure."""
    text = _http_get_text(url, headers=headers, timeout=timeout)
    if text:
        try:
            return json.loads(text)
        except Exception:
            pass
    return None


def _songs_from_raw_lines(lines: list) -> list:
    """Parse a list of 'Title - Artist' strings into song dicts."""
    songs = []
    for line in lines:
        song = _line_to_song(line)
        if song:
            songs.append(song)
    return songs


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE 1 (Priority): yt-dlp  ★80k+ — handles YT/YTMusic/Spotify/SC/BC/etc.
# ─────────────────────────────────────────────────────────────────────────────

def _ytdlp_extract_songs(url: str, is_single: bool = False) -> list:
    """
    Use yt-dlp to extract track metadata from any supported URL.
    Supports: YouTube, YouTube Music, Spotify (via spotdl plugin or embed),
              SoundCloud, Bandcamp, Deezer, Tidal, and 1000+ more sites.

    Returns list of song dicts.  Never downloads audio here.
    """
    if not HAS_YTDLP:
        return []
    ydl_opts = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  "in_playlist" if not is_single else False,
        "skip_download": True,
        "ignoreerrors":  True,
        # China network optimization
        "socket_timeout":  30,
        "retries":         5,
        "extractor_args": {
            "youtube": {"player_client": ["android_music", "tv_embedded", "web"]},
        },
    }
    if PROXY:
        ydl_opts["proxy"] = PROXY
    # Provide ffmpeg location for audio conversion
    ffmpeg_exe = _find_ffmpeg()
    if ffmpeg_exe:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_exe).parent)

    songs = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return []

            # Playlist / channel
            entries = info.get("entries")
            if entries:
                for entry in entries:
                    if not entry:
                        continue
                    title    = (entry.get("title") or "").strip()
                    uploader = (entry.get("uploader") or entry.get("channel") or "").strip()
                    artist   = (entry.get("artist") or uploader or "Unknown").strip()
                    # Many YT titles are already "Artist - Title"
                    if " - " in title:
                        song = _line_to_song(title)
                    else:
                        song = _line_to_song(f"{title} - {artist}")
                    if song and title:
                        songs.append(song)
            else:
                # Single track
                title    = (info.get("title") or "").strip()
                uploader = (info.get("uploader") or info.get("channel") or "").strip()
                artist   = (info.get("artist") or uploader or "Unknown").strip()
                if " - " in title:
                    song = _line_to_song(title)
                else:
                    song = _line_to_song(f"{title} - {artist}")
                if song and title:
                    songs.append(song)
    except Exception as e:
        log.debug(f"[YTDLP] extract_songs error for {url}: {e}")
    return songs


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE 2: QQ Music ──────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _qqmusic_playlist_id_from_url(url: str) -> Optional[str]:
    """Extract numeric playlist id from a QQ Music URL."""
    if "c6.y.qq.com" in url or "/__=" in url or re.search(r"y\.qq\.com.*\?.*__=", url):
        url = _resolve_short_url(url)
    m = re.search(r"/playlist/(\d+)", url)
    if m:
        return m.group(1)
    m2 = re.search(r"[?&](?:id|disstid)=(\d+)", url)
    if m2:
        return m2.group(1)
    return None


def _fetch_qqmusic_playlist(url: str) -> list:
    log.info(f"[PLAYLIST/QQ] Fetching: {url}")
    playlist_id = _qqmusic_playlist_id_from_url(url)
    if not playlist_id:
        log.warning(f"[PLAYLIST/QQ] Could not extract playlist ID from: {url}")
        return []
    log.info(f"[PLAYLIST/QQ] Playlist ID: {playlist_id}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    f"https://y.qq.com/n/ryqq/playlist/{playlist_id}",
        "Cookie":     "uin=0; fqm_pvqid=1; pgv_pvid=1; pgv_info=ssid=;",
        "Accept":     "application/json, text/plain, */*",
    }

    def _parse_songlist(raw_songs):
        songs = []
        for s in raw_songs:
            title = (s.get("songname") or s.get("name") or s.get("title") or "").strip()
            if not title:
                continue
            singers = s.get("singer") or s.get("artists") or []
            if isinstance(singers, list):
                artist = " / ".join(
                    (sg.get("name") or sg.get("title") or "").strip()
                    for sg in singers
                    if (sg.get("name") or sg.get("title") or "").strip()
                )
            else:
                artist = str(singers).strip()
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            if song:
                songs.append(song)
        return songs

    raw_songs = []

    # API v1 — classic fcg endpoint (most stable)
    api1 = (
        "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
        f"?type=1&json=1&utf8=1&onlysong=0&disstid={playlist_id}"
        "&format=json&inCharset=utf8&outCharset=utf-8&g_tk=5381&loginUin=0&hostUin=0"
        "&platform=yqq.json&needNewCode=0"
    )
    data = _http_get_json_h(api1, headers=headers)
    if data and data.get("cdlist"):
        raw_songs = data["cdlist"][0].get("songlist") or []
        log.debug(f"[PLAYLIST/QQ] API v1 → {len(raw_songs)} raw songs")

    # API v2 — musicu POST with proper JSON payload (2024 standard)
    if not raw_songs:
        payload_v2 = json.dumps({
            "req_1": {
                "module": "music.srfDissInfo.aiDissInfo",
                "method": "uniform_get_Dissinfo",
                "param":  {
                    "disstid":  int(playlist_id),
                    "onlysong": 0,
                    "num":      500,
                    "begin":    0,
                    "song_begin": 0,
                    "song_num":   500,
                },
            }
        }, ensure_ascii=False)
        api2_url = "https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&inCharset=utf8&outCharset=utf-8&data=" + urllib.parse.quote(payload_v2)
        d2 = _http_get_json_h(api2_url, headers=headers)
        if d2:
            raw_songs = ((d2.get("req_1") or {}).get("data") or {}).get("songlist") or []
            log.debug(f"[PLAYLIST/QQ] API v2 → {len(raw_songs)} raw songs")

    # API v3 — PlaylistSongs server
    if not raw_songs:
        payload_v3 = json.dumps({
            "req_1": {
                "module": "music.PlaylistSongsServer.GetPlaylistSongs",
                "method": "GetPlaylistSongs",
                "param":  {"disstid": int(playlist_id), "num": 500, "begin": 0},
            }
        }, ensure_ascii=False)
        api3_url = "https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&inCharset=utf8&outCharset=utf-8&data=" + urllib.parse.quote(payload_v3)
        d3 = _http_get_json_h(api3_url, headers=headers)
        if d3:
            raw_songs = ((d3.get("req_1") or {}).get("data") or {}).get("songlist") or []
            log.debug(f"[PLAYLIST/QQ] API v3 → {len(raw_songs)} raw songs")

    # API v4 — musics.fcg (2024+ new endpoint) with comm block
    if not raw_songs:
        payload_v4 = json.dumps({
            "comm": {
                "cv": 4747474, "ct": 24, "format": "json",
                "inCharset": "utf-8", "outCharset": "utf-8", "os_ver": "12",
                "platform": "yqq.json", "patch": 0, "wid": 0,
                "g_tk": 5381, "loginUin": 0, "hostUin": 0,
            },
            "req_1": {
                "module": "music.PlaylistSongsServer.GetPlaylistSongs",
                "method": "GetPlaylistSongs",
                "param":  {"disstid": int(playlist_id), "num": 500, "begin": 0},
            }
        }, ensure_ascii=False)
        api4_url = "https://u.y.qq.com/cgi-bin/musics.fcg?_=1&data=" + urllib.parse.quote(payload_v4)
        text4 = _http_get_text(api4_url, headers=headers)
        if text4:
            try:
                d4 = json.loads(text4)
                raw_songs = ((d4.get("req_1") or {}).get("data") or {}).get("songlist") or []
                log.debug(f"[PLAYLIST/QQ] API v4 → {len(raw_songs)} raw songs")
            except Exception:
                pass

    # API v5 — webpage HTML embed scrape (最后手段)
    if not raw_songs:
        log.debug("[PLAYLIST/QQ] All APIs failed, trying HTML page scrape ...")
        html = _http_get_text(
            f"https://y.qq.com/n/ryqq/playlist/{playlist_id}",
            headers={**headers, "Accept": "text/html"},
        )
        if html:
            # Extract __INITIAL_DATA__ or window.__DATA__
            for pat in [
                r'window\.__INITIAL_DATA__\s*=\s*({.*?})\s*;?\s*</script>',
                r'"songList"\s*:\s*(\[.*?\])\s*[,}]',
                r'"tracks"\s*:\s*(\[.*?\])',
            ]:
                m = re.search(pat, html, re.DOTALL)
                if m:
                    try:
                        chunk = m.group(1)
                        if chunk.startswith("{"):
                            obj = json.loads(chunk)
                            raw_songs = (
                                obj.get("songList") or obj.get("tracks") or
                                (obj.get("detail") or {}).get("songList") or []
                            )
                        else:
                            raw_songs = json.loads(chunk)
                        if raw_songs:
                            log.debug(f"[PLAYLIST/QQ] HTML scrape → {len(raw_songs)} raw songs")
                            break
                    except Exception:
                        pass

    songs = _parse_songlist(raw_songs)
    log.info(f"[PLAYLIST/QQ] Got {len(songs)} songs")
    return songs


# ── NetEase Cloud Music (网易云音乐) ──────────────────────────────────────────

def _fetch_netease_playlist(url: str) -> list:
    log.info(f"[PLAYLIST/NetEase] Fetching: {url}")
    if "163cn.tv" in url or "163cn" in url:
        url = _resolve_short_url(url)
    m = (re.search(r"/playlist[/?].*?[?&]id=(\d+)", url) or
         re.search(r"/playlist/(\d+)", url) or
         re.search(r"[?&]id=(\d+)", url))
    if not m:
        log.warning(f"[PLAYLIST/NetEase] Could not extract playlist ID from: {url}")
        return []
    playlist_id = m.group(1)
    log.info(f"[PLAYLIST/NetEase] Playlist ID: {playlist_id}")

    headers_pc = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://music.163.com/",
        "Cookie":     (
            "appver=8.9.20; os=pc; channel=netease; "
            "NMTID=00OHd3KFzRy1FOdS0nA; "
            "_ntes_nnid=xxx; _ntes_nuid=xxx; "
        ),
        "Accept":     "application/json, text/plain, */*",
    }
    headers_mobile = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/112.0.0.0 Mobile Safari/537.36"
        ),
        "Referer": "https://music.163.com/",
        "Cookie":  "os=android; appver=8.10.00;",
    }

    def _parse_tracks(tracks):
        songs = []
        for t in tracks:
            title = (t.get("name") or "").strip()
            if not title:
                continue
            ar = t.get("artists") or t.get("ar") or []
            artist = " / ".join(
                (a.get("name") or "").strip() for a in ar
                if (a.get("name") or "").strip()
            ) or "Unknown"
            song = _line_to_song(f"{title} - {artist}")
            if song:
                songs.append(song)
        return songs

    tracks = []
    d1, d2 = None, None

    # API v1: classic playlist detail
    d1 = _http_get_json_h(
        f"https://music.163.com/api/playlist/detail?id={playlist_id}",
        headers=headers_pc)
    if d1:
        pl = d1.get("result") or d1.get("playlist") or {}
        tracks = pl.get("tracks") or []
        log.debug(f"[PLAYLIST/NetEase] API v1 → {len(tracks)} tracks")

    # API v2: eapi v3 (returns more tracks, up to 1000)
    if not tracks:
        d2 = _http_get_json_h(
            f"https://music.163.com/eapi/v3/playlist/detail?id={playlist_id}&n=1000",
            headers=headers_pc)
        if d2:
            pl = d2.get("playlist") or {}
            tracks = pl.get("tracks") or []
            log.debug(f"[PLAYLIST/NetEase] API v2 → {len(tracks)} tracks")

    # API v3: mobile API endpoint
    if not tracks:
        d3m = _http_get_json_h(
            f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=1000&s=0",
            headers=headers_mobile)
        if d3m:
            pl = d3m.get("playlist") or {}
            tracks = pl.get("tracks") or []
            log.debug(f"[PLAYLIST/NetEase] API v3(mobile) → {len(tracks)} tracks")

    # Fallback: only track IDs returned — fetch song details in batch
    if not tracks:
        pl2 = ((d1 or {}).get("result") or (d1 or {}).get("playlist") or
               (d2 or {}).get("playlist") or {})
        track_id_objs = pl2.get("trackIds") or []
        tid_list = []
        for t in track_id_objs:
            if isinstance(t, dict):
                tid_list.append(str(t.get("id") or ""))
            else:
                tid_list.append(str(t))
        tid_list = [x for x in tid_list if x]
        if tid_list:
            log.info(f"[PLAYLIST/NetEase] Resolving {len(tid_list)} track IDs ...")
            # Batch in groups of 200 to avoid URL length limit
            for batch_start in range(0, min(len(tid_list), 1000), 200):
                batch = tid_list[batch_start:batch_start + 200]
                ids_str = urllib.parse.quote(json.dumps(
                    [{"id": int(x)} for x in batch]
                ))
                d_batch = _http_get_json_h(
                    f"https://music.163.com/api/song/detail/?ids={ids_str}",
                    headers=headers_pc)
                if d_batch:
                    tracks.extend(d_batch.get("songs") or [])
            log.debug(f"[PLAYLIST/NetEase] Batch song detail → {len(tracks)} tracks")

    songs = _parse_tracks(tracks)
    log.info(f"[PLAYLIST/NetEase] Got {len(songs)} songs")
    return songs


# ─────────────────────────────────────────────────────────────────────────────
# Engines 4-8: Kugou / Kuwo / Qishui / Migu / Apple Music + single-track parsers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_kugou_playlist(url: str) -> list:
    log.info(f"[PLAYLIST/Kugou] Fetching: {url}")
    # https://www.kugou.com/yy/special/single/XXXXXXX.html
    # https://www.kugou.com/share/...#hash=XXX&album_id=XXX
    m = re.search(r"/special/single/(\d+)", url)
    if not m:
        m = re.search(r"[?&/](?:special_id|id)=(\d+)", url)
    if not m:
        m = re.search(r"/(\d+)\.html", url)
    if not m:
        log.warning(f"[PLAYLIST/Kugou] Could not extract playlist ID from: {url}")
        return []
    playlist_id = m.group(1)
    log.info(f"[PLAYLIST/Kugou] Playlist ID: {playlist_id}")

    api = (
        f"https://www.kugou.com/yy/index.php?r=play/getdata"
        f"&hash=&album_id={playlist_id}&mid=&platid=4"
    )
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.kugou.com/",
    }

    # Try songlist API directly
    api2 = (
        f"https://m3ws.kugou.com/api/app/special/song?specialid={playlist_id}"
        f"&page=1&pagesize=500&token=&userid=0&mid=0"
    )
    data = _http_get_json_h(api2, headers=headers)
    songs = []
    if data:
        items = (data.get("data") or {}).get("info") or []
        for item in items:
            title  = (item.get("songname") or item.get("filename") or "").strip()
            artist = (item.get("singername") or "").strip()
            if not title:
                continue
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            if song:
                songs.append(song)

    if not songs:
        # Fallback: scrape playlist page
        html = _http_get_text(
            f"https://www.kugou.com/yy/special/single/{playlist_id}.html",
            headers=headers
        )
        if html:
            # Extract JSON data embedded in page
            m2 = re.search(r'"list"\s*:\s*(\[.*?\])\s*[,}]', html, re.DOTALL)
            if m2:
                try:
                    items = json.loads(m2.group(1))
                    for item in items:
                        title  = (item.get("filename") or item.get("songname") or "").strip()
                        artist = (item.get("singername") or "").strip()
                        if not title:
                            continue
                        # kugou filename often is "Artist - Title"
                        if " - " in title and not artist:
                            parts = title.split(" - ", 1)
                            artist, title = parts[0].strip(), parts[1].strip()
                        song = _line_to_song(f"{title} - {artist or 'Unknown'}")
                        if song:
                            songs.append(song)
                except Exception:
                    pass

    log.info(f"[PLAYLIST/Kugou] Got {len(songs)} songs")
    return songs


# ── Kuwo Music (酷我音乐) ─────────────────────────────────────────────────────

def _fetch_kuwo_playlist(url: str) -> list:
    log.info(f"[PLAYLIST/Kuwo] Fetching: {url}")
    # https://www.kuwo.cn/playlist_detail/XXXXXXXXX
    m = re.search(r"/playlist[_/]detail[_/]?(\d+)", url)
    if not m:
        m = re.search(r"[?&/](?:pid|id)=(\d+)", url)
    if not m:
        m = re.search(r"/(\d+)(?:[?#]|$)", url)
    if not m:
        log.warning(f"[PLAYLIST/Kuwo] Could not extract playlist ID from: {url}")
        return []
    playlist_id = m.group(1)
    log.info(f"[PLAYLIST/Kuwo] Playlist ID: {playlist_id}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.kuwo.cn/",
        "csrf":       "wvJL",
        "Cookie":     "kw_token=wvJL",
    }
    api = (
        f"https://www.kuwo.cn/api/www/playlist/playListInfo"
        f"?pid={playlist_id}&pn=1&rn=500&httpsStatus=1&reqId=0"
    )
    data = _http_get_json_h(api, headers=headers)
    songs = []
    if data:
        items = (data.get("data") or {}).get("musicList") or []
        for item in items:
            title  = (item.get("name") or "").strip()
            artist = (item.get("artist") or "").strip()
            if not title:
                continue
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            if song:
                songs.append(song)
    log.info(f"[PLAYLIST/Kuwo] Got {len(songs)} songs")
    return songs


# ── 汽水音乐 (Qishui / Douyin Music) ─────────────────────────────────────────

def _fetch_qishui_playlist(url: str) -> list:
    log.info(f"[PLAYLIST/Qishui] Fetching: {url}")
    # https://music.douyin.com/qishui/share/playlist?playlist_id=XXXXXXXX
    # https://www.qishui.com/playlist/XXXXXXXX
    m = re.search(r"playlist[_/]?(?:id=|/)([A-Za-z0-9_-]+)", url)
    if not m:
        m = re.search(r"[?&](?:id|playlist_id)=([A-Za-z0-9_-]+)", url)
    if not m:
        log.warning(f"[PLAYLIST/Qishui] Could not extract playlist ID from: {url}")
        return []
    playlist_id = m.group(1)
    log.info(f"[PLAYLIST/Qishui] Playlist ID: {playlist_id}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://music.douyin.com/",
    }
    api = (
        f"https://music.douyin.com/api/playlist/songs"
        f"?playlist_id={playlist_id}&count=500&cursor=0"
    )
    data = _http_get_json_h(api, headers=headers)
    songs = []
    if data:
        items = (data.get("data") or {}).get("songs") or []
        for item in items:
            title  = (item.get("name") or item.get("title") or "").strip()
            artists_raw = item.get("artists") or item.get("author") or []
            if isinstance(artists_raw, list):
                artist = " / ".join(
                    (a.get("name") or "").strip()
                    for a in artists_raw
                    if (a.get("name") or "").strip()
                )
            else:
                artist = str(artists_raw).strip()
            if not title:
                continue
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            if song:
                songs.append(song)

    if not songs:
        log.warning("[PLAYLIST/Qishui] API returned no songs (may need login). Trying page scrape ...")
        html = _http_get_text(url, headers=headers)
        if html:
            # Look for JSON blobs
            for pattern in [r'"song_list"\s*:\s*(\[.*?\])', r'"songs"\s*:\s*(\[.*?\])']:
                m2 = re.search(pattern, html, re.DOTALL)
                if m2:
                    try:
                        items = json.loads(m2.group(1))
                        for item in items:
                            title  = (item.get("name") or item.get("title") or "").strip()
                            artist = (item.get("artist") or "").strip()
                            if title:
                                song = _line_to_song(f"{title} - {artist or 'Unknown'}")
                                if song:
                                    songs.append(song)
                        if songs:
                            break
                    except Exception:
                        pass

    log.info(f"[PLAYLIST/Qishui] Got {len(songs)} songs")
    return songs


# ── 咪咕音乐 (Migu Music) ─────────────────────────────────────────────────────

def _fetch_migu_playlist(url: str) -> list:
    log.info(f"[PLAYLIST/Migu] Fetching: {url}")
    # https://music.migu.cn/v3/music/playlist/XXXXXXXXXX
    m = re.search(r"/playlist[s/]+(\d+)", url)
    if not m:
        m = re.search(r"[?&/](?:id|listId)=(\d+)", url)
    if not m:
        log.warning(f"[PLAYLIST/Migu] Could not extract playlist ID from: {url}")
        return []
    playlist_id = m.group(1)
    log.info(f"[PLAYLIST/Migu] Playlist ID: {playlist_id}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://music.migu.cn/",
        "Origin":     "https://music.migu.cn",
    }
    api = (
        f"https://music.migu.cn/v3/api/music/audioPlayer/songs"
        f"?songListId={playlist_id}&page=1&pageSize=500"
    )
    data = _http_get_json_h(api, headers=headers)
    songs = []
    if data:
        items = (data.get("data") or {}).get("songList") or data.get("songs") or []
        for item in items:
            title  = (item.get("songName") or item.get("name") or "").strip()
            artist = (item.get("singerName") or item.get("artist") or "").strip()
            if not title:
                continue
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            if song:
                songs.append(song)
    log.info(f"[PLAYLIST/Migu] Got {len(songs)} songs")
    return songs


# ── Spotify ───────────────────────────────────────────────────────────────────

def _fetch_spotify_playlist(url: str) -> list:
    """
    Fetch Spotify playlist songs.
    Priority:
      1. yt-dlp (★80k) — most reliable, handles Spotify natively via redirect
      2. Spotify embed page scrape (no auth)
      3. spotdl CLI (if installed)
    """
    log.info(f"[PLAYLIST/Spotify] Fetching: {url}")

    m = re.search(r"spotify\.com/(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]+)", url)
    if not m:
        log.warning(f"[PLAYLIST/Spotify] Could not extract playlist ID from: {url}")
        return []
    playlist_id = m.group(1)
    log.info(f"[PLAYLIST/Spotify] Playlist ID: {playlist_id}")

    songs = []

    # Strategy 1: yt-dlp (most reliable for Spotify — uses redirect to YouTube)
    if HAS_YTDLP:
        songs = _ytdlp_extract_songs(url)
        if songs:
            log.info(f"[PLAYLIST/Spotify] Got {len(songs)} songs via yt-dlp")
            return songs

    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

    # Strategy 2: Spotify embed page scrape
    for fetch_url in [
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
        f"https://open.spotify.com/playlist/{playlist_id}",
    ]:
        html = _http_get_text(fetch_url, headers=headers, timeout=30)
        if not html:
            continue
        m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m2:
            try:
                jdata = json.loads(m2.group(1))
                # Walk the entire JSON looking for track entities
                text_dump = json.dumps(jdata)
                # Extract track objects: {"type":"track","name":"...","artists":[...]}
                for match in re.finditer(
                    r'"type"\s*:\s*"track"\s*,\s*"uid"\s*:\s*"[^"]*"\s*,\s*"title"\s*:\s*"([^"]+)"',
                    text_dump
                ):
                    title = match.group(1)
                    song = _line_to_song(f"{title} - Unknown")
                    if song:
                        songs.append(song)
                if not songs:
                    entities = (((jdata.get("props") or {}).get("pageProps") or {})
                                .get("state") or {})
                    for val in (entities.get("entities") or {}).get("items", {}).values():
                        if isinstance(val, dict) and val.get("type") == "track":
                            title = (val.get("name") or "").strip()
                            artist = " / ".join(
                                (a.get("name") or "").strip()
                                for a in (val.get("artists") or [])
                                if (a.get("name") or "").strip()
                            )
                            if title:
                                song = _line_to_song(f"{title} - {artist or 'Unknown'}")
                                if song:
                                    songs.append(song)
            except Exception as e:
                log.debug(f"[PLAYLIST/Spotify] JSON parse: {e}")
        if songs:
            break

    # Strategy 3: spotdl CLI
    if not songs:
        spotdl = shutil.which("spotdl")
        if spotdl:
            try:
                result = subprocess.run(
                    [spotdl, "save", url, "--save-file", "-"],
                    capture_output=True, text=True, timeout=90,
                    encoding="utf-8", errors="replace"
                )
                for line in result.stdout.splitlines():
                    song = _line_to_song(line.strip())
                    if song:
                        songs.append(song)
            except Exception as e:
                log.debug(f"[PLAYLIST/Spotify] spotdl: {e}")

    log.info(f"[PLAYLIST/Spotify] Got {len(songs)} songs")
    return songs


# ── YouTube Music ─────────────────────────────────────────────────────────────

def _fetch_ytmusic_playlist(url: str) -> list:
    """
    Fetch YouTube Music / YouTube playlist songs via yt-dlp.
    yt-dlp is the primary and only needed engine here.
    """
    log.info(f"[PLAYLIST/YTMusic] Fetching: {url}")
    # Normalize music.youtube.com -> youtube.com
    list_m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if not list_m:
        log.warning(f"[PLAYLIST/YTMusic] Could not extract list ID from: {url}")
        return []
    yt_url = f"https://www.youtube.com/playlist?list={list_m.group(1)}"
    log.info(f"[PLAYLIST/YTMusic] List ID: {list_m.group(1)}")
    songs = _ytdlp_extract_songs(yt_url)
    log.info(f"[PLAYLIST/YTMusic] Got {len(songs)} songs")
    return songs


# ── Apple Music ───────────────────────────────────────────────────────────────

def _fetch_apple_music_playlist(url: str) -> list:
    """
    Fetch Apple Music playlist songs.
    Strategy: scrape the oEmbed / embed page (no API key needed for public playlists).
    """
    log.info(f"[PLAYLIST/AppleMusic] Fetching: {url}")

    # Extract playlist or album ID
    # https://music.apple.com/us/playlist/name/pl.XXXXXX
    m = re.search(r"music\.apple\.com/[a-z]+/(?:playlist|album)/[^/]+/([A-Za-z0-9.]+)", url)
    if not m:
        log.warning(f"[PLAYLIST/AppleMusic] Could not extract ID from: {url}")
        return []
    resource_id = m.group(1)
    log.info(f"[PLAYLIST/AppleMusic] Resource ID: {resource_id}")

    headers = {
        "User-Agent":      _BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Try oEmbed JSON API
    oembed_url = f"https://music.apple.com/oembed?url={urllib.parse.quote(url)}&format=json"
    data = _http_get_json_h(oembed_url, headers=headers)

    songs = []

    # Scrape the page HTML
    html = _http_get_text(url, headers=headers, timeout=25)
    if html:
        # Look for JSON-LD structured data
        ld_blocks = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                               html, re.DOTALL)
        for block in ld_blocks:
            try:
                ld = json.loads(block)
                track_list = ld.get("track") or []
                for t in track_list:
                    title  = (t.get("name") or "").strip()
                    artist = ""
                    by_artist = t.get("byArtist") or {}
                    if isinstance(by_artist, dict):
                        artist = (by_artist.get("name") or "").strip()
                    elif isinstance(by_artist, list):
                        artist = " / ".join(
                            (a.get("name") or "").strip() for a in by_artist
                            if (a.get("name") or "").strip()
                        )
                    if title:
                        song = _line_to_song(f"{title} - {artist or 'Unknown'}")
                        if song:
                            songs.append(song)
            except Exception:
                pass

        if not songs:
            # Fallback: extract from Next.js data
            m2 = re.search(r'<script id="schema:music-player"[^>]*>(.*?)</script>',
                           html, re.DOTALL)
            if not m2:
                m2 = re.search(r'ampData\s*=\s*({.*?});</script>', html, re.DOTALL)
            if m2:
                try:
                    jd = json.loads(m2.group(1))
                    tracks = jd.get("track") or jd.get("tracks") or []
                    for t in tracks:
                        title  = (t.get("name") or t.get("title") or "").strip()
                        artist = (t.get("artistName") or "").strip()
                        if title:
                            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
                            if song:
                                songs.append(song)
                except Exception:
                    pass

    log.info(f"[PLAYLIST/AppleMusic] Got {len(songs)} songs")
    return songs


# ── Universal dispatcher ───────────────────────────────────────────────────────

def _detect_platform(url: str) -> str:
    """Detect which music platform the URL belongs to."""
    u = url.lower()
    if "y.qq.com" in u or "c6.y.qq.com" in u or "c.y.qq.com" in u:
        return "qqmusic"
    if "music.163.com" in u or "163cn.tv" in u:
        return "netease"
    if "kugou.com" in u:
        return "kugou"
    if "kuwo.cn" in u:
        return "kuwo"
    if "music.douyin.com" in u or "qishui.com" in u or "qishui" in u:
        return "qishui"
    if "music.migu.cn" in u or "migu.cn" in u:
        return "migu"
    if "open.spotify.com" in u or "spotify.com" in u:
        return "spotify"
    if "music.youtube.com" in u or "youtube.com" in u or "youtu.be" in u:
        return "ytmusic"
    if "music.apple.com" in u:
        return "applemusic"
    # yt-dlp covers these platforms natively
    if "soundcloud.com" in u:
        return "soundcloud"
    if "deezer.com" in u:
        return "deezer"
    if "tidal.com" in u:
        return "tidal"
    if "bandcamp.com" in u:
        return "bandcamp"
    if "amazon.com/music" in u or "music.amazon" in u:
        return "amazonmusic"
    return "unknown"


_PLATFORM_FETCHERS = {
    "qqmusic":    _fetch_qqmusic_playlist,
    "netease":    _fetch_netease_playlist,
    "kugou":      _fetch_kugou_playlist,
    "kuwo":       _fetch_kuwo_playlist,
    "qishui":     _fetch_qishui_playlist,
    "migu":       _fetch_migu_playlist,
    "spotify":    _fetch_spotify_playlist,
    "ytmusic":    _fetch_ytmusic_playlist,
    "applemusic": _fetch_apple_music_playlist,
    # yt-dlp-backed platforms
    "soundcloud": _ytdlp_extract_songs,
    "deezer":     _ytdlp_extract_songs,
    "tidal":      _ytdlp_extract_songs,
    "bandcamp":   _ytdlp_extract_songs,
    "amazonmusic":_ytdlp_extract_songs,
}

_PLATFORM_NAMES = {
    "qqmusic":    "QQ Music",
    "netease":    "NetEase Cloud Music (网易云)",
    "kugou":      "Kugou Music (酷狗)",
    "kuwo":       "Kuwo Music (酷我)",
    "qishui":     "Qishui Music (汽水音乐)",
    "migu":       "Migu Music (咪咕)",
    "spotify":    "Spotify",
    "ytmusic":    "YouTube / YouTube Music",
    "applemusic": "Apple Music",
    "soundcloud": "SoundCloud",
    "deezer":     "Deezer",
    "tidal":      "Tidal",
    "bandcamp":   "Bandcamp",
    "amazonmusic":"Amazon Music",
}

# ─────────────────────────────────────────────────────────────────────────────
# Multi-engine fallback chain ordered by GitHub stars / reliability
#
# Priority  Engine / Library            GitHub Stars  Notes
# ────────  ──────────────────────────  ────────────  ─────────────────────────
#  1        yt-dlp                       ★ 80k+       1000+ sites, most reliable
#  2        Platform-native API          n/a          CN platforms: exact match
#  3        spotdl                       ★ 16k        Spotify specialist
#  4        HTML/embed scrape            n/a          last resort, no deps
#
# For playlist parsing the strategy is:
#   Step 1 — if platform detected → try native fetcher first (fastest, exact)
#   Step 2 — if native returned 0 → try yt-dlp (catches most cases)
#   Step 3 — if yt-dlp returned 0 → try remaining platform fetchers (rare cross-platform)
# ─────────────────────────────────────────────────────────────────────────────

# Ordered list of ALL playlist fetchers tried for any unknown/failed URL
# (sorted by GitHub stars descending — yt-dlp first, then CN platforms, then global)
_FALLBACK_FETCHER_ORDER = [
    # ── yt-dlp ★80k ─────────────────────────────────────────────────────────
    ("ytdlp_universal",  _ytdlp_extract_songs),
    # ── CN platform-native APIs (no stars, but most accurate for CN links) ──
    ("qqmusic",          _fetch_qqmusic_playlist),
    ("netease",          _fetch_netease_playlist),
    ("kugou",            _fetch_kugou_playlist),
    ("kuwo",             _fetch_kuwo_playlist),
    ("qishui",           _fetch_qishui_playlist),
    ("migu",             _fetch_migu_playlist),
    # ── International platforms ──────────────────────────────────────────────
    ("spotify",          _fetch_spotify_playlist),
    ("applemusic",       _fetch_apple_music_playlist),
]


def fetch_playlist_from_url(url: str) -> list:
    """
    Universal playlist fetcher with multi-engine fallback chain.

    Strategy (按 GitHub 星标优先级逐个尝试):
      1. Detect platform from URL
      2. Try platform-native fetcher first (exact match, fastest)
      3. If 0 songs → try yt-dlp universal engine (★80k, covers 1000+ sites)
      4. If still 0 → walk the entire fallback chain until a fetcher returns songs
         (stops at first success — does NOT try remaining engines after success)

    All requests use system proxy and CN-optimized session.
    """
    url = url.strip()

    # Resolve short URLs first
    url_type = _detect_url_type(url)
    if url_type == "short":
        resolved = _resolve_short_url(url)
        if resolved != url:
            log.info(f"[PLAYLIST] Short URL resolved: {resolved}")
            url = resolved

    platform = _detect_platform(url)
    platform_name = _PLATFORM_NAMES.get(platform, platform)

    if platform == "unknown":
        resolved = _resolve_short_url(url)
        if resolved != url:
            url = resolved
            platform = _detect_platform(url)
            platform_name = _PLATFORM_NAMES.get(platform, "Unknown")

    log.info(f"[PLAYLIST] Platform detected: {platform_name}  url={url[:80]}")

    # ── Step 1: platform-native fetcher (highest accuracy for known platforms)
    native_fetcher = _PLATFORM_FETCHERS.get(platform)
    if native_fetcher and native_fetcher is not _ytdlp_extract_songs:
        try:
            songs = native_fetcher(url)
        except Exception as e:
            log.debug(f"[PLAYLIST] Native fetcher error ({platform_name}): {e}")
            songs = []
        if songs:
            log.info(f"[PLAYLIST] {platform_name} native API → {len(songs)} songs  [engine 1/4]")
            return songs
        log.warning(f"[PLAYLIST] {platform_name} native API returned 0 songs → trying fallback chain")

    # ── Step 2: yt-dlp universal engine (★80k+ — highest priority after native)
    if HAS_YTDLP:
        try:
            songs = _ytdlp_extract_songs(url)
        except Exception as e:
            log.debug(f"[PLAYLIST] yt-dlp error: {e}")
            songs = []
        if songs:
            log.info(f"[PLAYLIST] yt-dlp universal → {len(songs)} songs  [engine 2/4]")
            return songs
        log.warning("[PLAYLIST] yt-dlp returned 0 songs → trying remaining platform parsers")

    # ── Step 3: walk entire fallback chain (skip already-tried engines)
    tried = {platform, "ytdlp_universal"}
    for engine_name, fetcher_fn in _FALLBACK_FETCHER_ORDER:
        if engine_name in tried:
            continue
        tried.add(engine_name)
        log.info(f"[PLAYLIST] Trying fallback engine: {_PLATFORM_NAMES.get(engine_name, engine_name)}")
        try:
            songs = fetcher_fn(url)
        except Exception as e:
            log.debug(f"[PLAYLIST] Fallback {engine_name} error: {e}")
            songs = []
        if songs:
            log.info(f"[PLAYLIST] {_PLATFORM_NAMES.get(engine_name, engine_name)} → {len(songs)} songs  [fallback]")
            return songs

    log.warning(
        "[PLAYLIST] ALL engines returned 0 songs.\n"
        "  Possible causes:\n"
        "    • Playlist is private / requires login\n"
        "    • URL format not recognized\n"
        "    • Platform API changed (CN platforms change APIs frequently)\n"
        "  Tip: copy songs manually to playlist.txt in 'Title - Artist' format."
    )
    return []


# Keep backward-compatible alias
def fetch_qqmusic_playlist(url: str) -> list:
    return fetch_playlist_from_url(url)


# ─────────────────────────────────────────────────────────────────────────────
# Single-track URL parser
# Detects whether a URL is a single song link and returns a 1-element song list
# Supports: QQ Music, NetEase, Kugou, Kuwo, Spotify, YouTube, Apple Music,
#           Qishui/Douyin, Migu
# ─────────────────────────────────────────────────────────────────────────────

def _detect_url_type(url: str) -> str:
    """
    Return 'playlist', 'track', or 'unknown'.
    Checks common URL patterns to distinguish single-song pages from playlist pages.
    """
    u = url.lower()
    # ── Explicit playlist patterns ──────────────────────────────────────────
    if re.search(r"/(playlist|special|songlist|disstid|collection)", u):
        return "playlist"
    if re.search(r"[?&](disstid|playlist_id|list=pl)", u):
        return "playlist"
    if "open.spotify.com/playlist" in u:
        return "playlist"
    if "music.youtube.com/playlist" in u or ("youtube.com/playlist" in u):
        return "playlist"
    if "music.apple.com" in u and "/playlist/" in u:
        return "playlist"
    if "music.163.com" in u and "playlist" in u:
        return "playlist"
    if "kuwo.cn" in u and "playlist" in u:
        return "playlist"
    if "kugou.com" in u and "special" in u:
        return "playlist"
    if "music.migu.cn" in u and "playlist" in u:
        return "playlist"
    # ── Explicit single-track patterns ──────────────────────────────────────
    if re.search(r"y\.qq\.com.*/song/", u):
        return "track"
    if re.search(r"music\.163\.com.*/song", u):
        return "track"
    if re.search(r"kugou\.com.*/song", u):
        return "track"
    if re.search(r"kuwo\.cn.*/play_detail", u):
        return "track"
    if "open.spotify.com/track" in u:
        return "track"
    if re.search(r"youtube\.com/watch\?v=|youtu\.be/", u):
        return "track"
    if "music.youtube.com/watch" in u:
        return "track"
    if "music.apple.com" in u and re.search(r"/song/|/album/[^/]+/\d+$", u):
        return "track"
    if "music.douyin.com" in u and "song" in u:
        return "track"
    if "music.migu.cn" in u and re.search(r"/song|/music/", u):
        return "track"
    # ── Short links: resolve first, then re-detect ──────────────────────────
    if re.search(r"c6\.y\.qq\.com|163cn\.tv|t\.cn|dwz\.cn|suo\.im", u):
        return "short"
    return "unknown"


def _fetch_track_qqmusic(url: str) -> list:
    """Parse a QQ Music single-song URL -> [{title, artist, ...}]"""
    # https://y.qq.com/n/ryqq/songDetail/XXXXXXXX  or  ?songmid=xxx
    m = re.search(r"/songDetail/([A-Za-z0-9]+)", url)
    if not m:
        m = re.search(r"[?&]songmid=([A-Za-z0-9]+)", url)
    if not m:
        m = re.search(r"/song/([A-Za-z0-9]+)", url)
    if not m:
        log.warning(f"[TRACK/QQ] Cannot extract song ID from: {url}")
        return []
    songmid = m.group(1)
    api = (
        "https://u.y.qq.com/cgi-bin/musicu.fcg"
        "?format=json&inCharset=utf8&outCharset=utf-8"
        f"&data={{\"req_1\":{{\"module\":\"music.pf_song_detail_svr\","
        f"\"method\":\"get_song_detail_yqq\","
        f"\"param\":{{\"song_mid\":\"{songmid}\"}}}}}}"
    )
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://y.qq.com/"}
    data = _http_get_json_h(api, headers=headers)
    if data:
        info = ((data.get("req_1") or {}).get("data") or {}).get("track_info") or {}
        title = (info.get("name") or "").strip()
        singers = info.get("singer") or []
        artist = " / ".join(
            (s.get("name") or "").strip() for s in singers
            if (s.get("name") or "").strip()
        ) or "Unknown"
        if title:
            song = _line_to_song(f"{title} - {artist}")
            return [song] if song else []
    return []


def _fetch_track_netease(url: str) -> list:
    """Parse a NetEase single-song URL -> [{title, artist, ...}]"""
    m = re.search(r"[?&/](?:id=|song/)(\d+)", url)
    if not m:
        log.warning(f"[TRACK/NetEase] Cannot extract song ID from: {url}")
        return []
    song_id = m.group(1)
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://music.163.com/",
               "Cookie": "appver=8.0.0"}
    api = f"https://music.163.com/api/song/detail/?ids=[{song_id}]"
    data = _http_get_json_h(api, headers=headers)
    if data:
        songs_raw = data.get("songs") or []
        if songs_raw:
            t = songs_raw[0]
            title = (t.get("name") or "").strip()
            artist = " / ".join(
                (a.get("name") or "").strip()
                for a in (t.get("artists") or [])
                if (a.get("name") or "").strip()
            ) or "Unknown"
            if title:
                song = _line_to_song(f"{title} - {artist}")
                return [song] if song else []
    return []


def _fetch_track_spotify(url: str) -> list:
    """Parse a Spotify single track URL -> [{title, artist, ...}]"""
    m = re.search(r"spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]+)", url)
    if not m:
        return []
    track_id = m.group(1)
    # Use oEmbed (no auth needed)
    oembed = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
    data = _http_get_json_h(oembed)
    if data:
        title_raw = data.get("title") or ""
        # oEmbed title is usually "Track Name - Artist Name"
        if " - " in title_raw:
            song = _line_to_song(title_raw)
            return [song] if song else []
    # Fallback: scrape embed page
    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    html = _http_get_text(embed_url, headers={"User-Agent": _BROWSER_UA,
                                               "Accept-Language": "en-US"})
    if html:
        m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m2:
            try:
                jdata = json.loads(m2.group(1))
                entities = (((jdata.get("props") or {})
                             .get("pageProps") or {})
                            .get("state") or {})
                # Find track entity
                for val in (entities.get("entities") or {}).get("items", {}).values():
                    if isinstance(val, dict) and val.get("type") == "track":
                        title = (val.get("name") or "").strip()
                        artists = " / ".join(
                            (a.get("name") or "").strip()
                            for a in (val.get("artists") or [])
                            if (a.get("name") or "").strip()
                        )
                        if title:
                            song = _line_to_song(f"{title} - {artists or 'Unknown'}")
                            return [song] if song else []
            except Exception:
                pass
    return []


def _fetch_track_youtube(url: str) -> list:
    """Parse a YouTube / YouTube Music single video URL -> [{title, artist, ...}]"""
    if not HAS_YTDLP:
        return []
    try:
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": False,
        }
        if PROXY:
            ydl_opts["proxy"] = PROXY
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title    = (info.get("title") or "").strip()
                uploader = (info.get("uploader") or info.get("channel") or "").strip()
                artist   = (info.get("artist") or uploader or "Unknown").strip()
                if title:
                    line = title if " - " in title else f"{title} - {artist}"
                    song = _line_to_song(line)
                    return [song] if song else []
    except Exception as e:
        log.debug(f"[TRACK/YouTube] yt-dlp error: {e}")
    return []


def _fetch_track_apple(url: str) -> list:
    """Parse an Apple Music single song URL -> [{title, artist, ...}]"""
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    html = _http_get_text(url, headers=headers)
    if html:
        # JSON-LD structured data
        for block in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL
        ):
            try:
                ld = json.loads(block)
                if ld.get("@type") in ("MusicRecording", "Song"):
                    title  = (ld.get("name") or "").strip()
                    by_art = ld.get("byArtist") or {}
                    artist = (by_art.get("name") if isinstance(by_art, dict)
                              else str(by_art)).strip() or "Unknown"
                    if title:
                        song = _line_to_song(f"{title} - {artist}")
                        return [song] if song else []
            except Exception:
                pass
        # Fallback: og:title
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        if m:
            song = _line_to_song(m.group(1))
            return [song] if song else []
    return []


def _fetch_track_kugou(url: str) -> list:
    """Parse a Kugou single song URL -> [{title, artist, ...}]"""
    # https://www.kugou.com/song/#hash=XXX&album_id=XXX
    m = re.search(r"[#?&]hash=([A-Fa-f0-9]+)", url)
    if not m:
        m = re.search(r"/song/([A-Za-z0-9]+)", url)
    if not m:
        return []
    song_hash = m.group(1)
    api = (
        f"https://m3ws.kugou.com/api/app/audio/info"
        f"?hash={song_hash}&platid=4&mid=0&token=&userid=0"
    )
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://www.kugou.com/"}
    data = _http_get_json_h(api, headers=headers)
    if data:
        info = data.get("data") or {}
        title  = (info.get("song_name") or info.get("filename") or "").strip()
        artist = (info.get("singer_name") or "").strip()
        if title:
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            return [song] if song else []
    return []


def _fetch_track_kuwo(url: str) -> list:
    """Parse a Kuwo single song URL -> [{title, artist, ...}]"""
    m = re.search(r"/play_detail/(\d+)", url)
    if not m:
        m = re.search(r"[?&/](?:rid|id)=(\d+)", url)
    if not m:
        return []
    rid = m.group(1)
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": "https://www.kuwo.cn/",
        "csrf": "wvJL", "Cookie": "kw_token=wvJL",
    }
    api = f"https://www.kuwo.cn/api/www/music/musicInfo?mid={rid}&httpsStatus=1"
    data = _http_get_json_h(api, headers=headers)
    if data:
        info = data.get("data") or {}
        title  = (info.get("name") or "").strip()
        artist = (info.get("artist") or "").strip()
        if title:
            song = _line_to_song(f"{title} - {artist or 'Unknown'}")
            return [song] if song else []
    return []


def _fetch_track_qishui(url: str) -> list:
    """Parse a Qishui/Douyin single song URL -> [{title, artist, ...}]"""
    m = re.search(r"[?&/](?:song_id|id)=([A-Za-z0-9_-]+)", url)
    if not m:
        return []
    song_id = m.group(1)
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://music.douyin.com/"}
    api = f"https://music.douyin.com/api/song/info?song_id={song_id}"
    data = _http_get_json_h(api, headers=headers)
    if data:
        info = (data.get("data") or {}).get("song") or {}
        title  = (info.get("name") or info.get("title") or "").strip()
        artists_raw = info.get("artists") or []
        artist = " / ".join(
            (a.get("name") or "").strip()
            for a in (artists_raw if isinstance(artists_raw, list) else [])
            if (a.get("name") or "").strip()
        ) or "Unknown"
        if title:
            song = _line_to_song(f"{title} - {artist}")
            return [song] if song else []
    return []


def _fetch_track_migu(url: str) -> list:
    """Parse a Migu single song URL -> [{title, artist, ...}]"""
    m = re.search(r"/(?:song|music)/(\d+)", url)
    if not m:
        m = re.search(r"[?&](?:id|song_id)=(\d+)", url)
    if not m:
        return []
    song_id = m.group(1)
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": "https://music.migu.cn/",
        "Origin": "https://music.migu.cn",
    }
    api = f"https://music.migu.cn/v3/api/music/audioPlayer/songs?copyrightId={song_id}"
    data = _http_get_json_h(api, headers=headers)
    if data:
        songs_raw = (data.get("data") or {}).get("songList") or data.get("songs") or []
        if songs_raw:
            t = songs_raw[0]
            title  = (t.get("songName") or t.get("name") or "").strip()
            artist = (t.get("singerName") or t.get("artist") or "").strip()
            if title:
                song = _line_to_song(f"{title} - {artist or 'Unknown'}")
                return [song] if song else []
    return []


# Single-track fetcher dispatch table (mirrors _PLATFORM_FETCHERS)
_TRACK_FETCHERS = {
    "qqmusic":    _fetch_track_qqmusic,
    "netease":    _fetch_track_netease,
    "kugou":      _fetch_track_kugou,
    "kuwo":       _fetch_track_kuwo,
    "qishui":     _fetch_track_qishui,
    "migu":       _fetch_track_migu,
    "spotify":    _fetch_track_spotify,
    "ytmusic":    _fetch_track_youtube,
    "applemusic": _fetch_track_apple,
}


def fetch_url_songs(url: str) -> list:
    """
    Universal entry point: accepts both playlist URLs and single-track URLs.
    Returns a list of song dicts (1 item for single tracks, N items for playlists).

    Multi-engine fallback chain (按 GitHub 星标优先级):
      Single track:
        1. Platform-native track API  (fastest, exact metadata)
        2. yt-dlp                     (★80k+ — handles any video/music URL)
      Playlist:
        → delegates to fetch_playlist_from_url (full 4-step chain)
    """
    url = url.strip()

    # Handle short links first
    url_type = _detect_url_type(url)
    if url_type == "short":
        resolved = _resolve_short_url(url)
        if resolved != url:
            log.info(f"[URL] Short URL resolved: {resolved}")
            url = resolved
            url_type = _detect_url_type(url)

    platform = _detect_platform(url)

    if url_type == "track":
        log.info(f"[URL] Detected single-track URL  platform={_PLATFORM_NAMES.get(platform, platform)}")

        # Engine 1: platform-native track parser
        fetcher = _TRACK_FETCHERS.get(platform)
        if fetcher:
            try:
                result = fetcher(url)
            except Exception as e:
                log.debug(f"[URL] Track fetcher error: {e}")
                result = []
            if result:
                log.info(f"[URL] Native track parser → {result[0].get('title','?')} - {result[0].get('artist','?')}")
                return result
            log.debug(f"[URL] Native track parser returned 0  → trying yt-dlp")

        # Engine 2: yt-dlp (handles YouTube, SoundCloud, Bandcamp, Spotify tracks, etc.)
        if HAS_YTDLP:
            try:
                result = _fetch_track_youtube(url)
            except Exception as e:
                log.debug(f"[URL] yt-dlp track error: {e}")
                result = []
            if result:
                log.info(f"[URL] yt-dlp → {result[0].get('title','?')} - {result[0].get('artist','?')}")
                return result

        # Engine 3: try as playlist (some short URLs resolve to playlists)
        log.warning(f"[URL] Single-track parsers returned 0, trying playlist parse as last resort")
        return fetch_playlist_from_url(url)

    else:
        # Treat as playlist (default) — full multi-engine fallback chain
        return fetch_playlist_from_url(url)


def _proxy_dict():
    if PROXY:
        return {"http": PROXY, "https": PROXY}
    return None


def http_get(url: str, timeout: int = 30, stream: bool = False):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
    }
    try:
        if HAS_REQUESTS:
            r = _requests.get(url, headers=headers, timeout=timeout,
                              proxies=_proxy_dict(), stream=stream)
            r.raise_for_status()
            return r if stream else r.content
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
    except Exception as e:
        log.debug(f"HTTP GET failed {url}: {e}")
        return None


def http_get_json(url: str):
    data = http_get(url)
    if isinstance(data, (bytes, bytearray)):
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# China network acceleration helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_aria2c() -> str:
    """
    Locate aria2c executable.
    Search order: ARIA2C_EXE global → PATH → common install paths.
    Returns empty string if not found.
    """
    global ARIA2C_EXE
    if ARIA2C_EXE and Path(ARIA2C_EXE).exists():
        return ARIA2C_EXE
    # Try PATH
    found = shutil.which("aria2c")
    if found:
        ARIA2C_EXE = found
        return found
    # Windows common paths
    for p in [
        Path(os.environ.get("LOCALAPPDATA", "")) / "aria2" / "aria2c.exe",
        Path("C:/aria2/aria2c.exe"),
        Path(__file__).parent / "aria2c.exe",
    ]:
        if p.exists():
            ARIA2C_EXE = str(p)
            return str(p)
    return ""


def _find_ffmpeg() -> str:
    """Locate ffmpeg executable. Returns empty string if not found."""
    global FFMPEG_EXE
    if FFMPEG_EXE and Path(FFMPEG_EXE).exists():
        return FFMPEG_EXE
    # Try PATH
    found = shutil.which("ffmpeg")
    if found:
        FFMPEG_EXE = found
        return found
    # Windows common paths
    for p in [
        Path("D:/ffmpeg/ffmpeg-8.0-essentials_build/bin/ffmpeg.exe"),
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path(os.environ.get("PROGRAMFILES", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(__file__).parent / "ffmpeg.exe",
    ]:
        if p.exists():
            FFMPEG_EXE = str(p)
            return str(p)
    return ""


def _convert_to_flac(input_path: Path, output_path: Path) -> bool:
    """
    Convert any audio file to FLAC format using ffmpeg.
    Returns True if conversion succeeded and output file exists.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        log.warning("  [FFMPEG] ffmpeg not found — cannot convert to FLAC")
        return False
    if not input_path or not input_path.exists():
        return False
    try:
        # Remove output if exists
        output_path.unlink(missing_ok=True)
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(input_path),
             "-c:a", "flac", "-compression_level", "5",
             str(output_path)],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
            # Remove original non-FLAC file
            if input_path.suffix.lower() != ".flac":
                input_path.unlink(missing_ok=True)
            return True
        else:
            log.debug(f"  [FFMPEG] Conversion failed: {result.stderr[-200:] if result.stderr else 'unknown'}")
            output_path.unlink(missing_ok=True)
            return False
    except Exception as e:
        log.debug(f"  [FFMPEG] Error: {e}")
        output_path.unlink(missing_ok=True)
        return False


def _cn_accelerate_url(url: str) -> str:
    """
    Optionally rewrite a URL to use a China-friendly CDN/mirror node.
    Currently: passes through most URLs unchanged (HK/SG PoPs already fast).
    For GitHub release URLs: rewrites through the fastest available CN mirror.
    """
    if not CN_ACCELERATE:
        return url
    # GitHub releases / raw content — rewrite through CN mirror
    if "github.com" in url or "githubusercontent.com" in url:
        for mirror in _GITHUB_CN_MIRRORS:
            return mirror + url  # just prepend mirror prefix
    return url


def _probe_aria2c_version(exe: str) -> bool:
    """Return True if aria2c is functional."""
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except Exception:
        return False


def _aria2c_download(url: str, dest: Path,
                     connections: int = None,
                     timeout: int = 120) -> bool:
    """
    Download a file using aria2c with multiple parallel connections.

    国内网络加速方案 (免费):
      - 16 并发分片连接：充分利用国内带宽，下载海外资源速度提升 3-8x
      - 断点续传：自动检测已下载部分，网络中断后继续
      - 自动重试：最多 10 次，退避 5 秒
      - 连接超时 60 秒：适应国内 -> 海外高延迟

    Falls back to requests if aria2c is unavailable.
    Returns True on success.
    """
    exe = _find_aria2c()
    if not exe or not _probe_aria2c_version(exe):
        return False  # caller will use requests fallback

    if connections is None:
        connections = ARIA2C_CONNECTIONS

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest_dir  = str(dest.parent)
    dest_name = dest.name

    # Build aria2c command
    cmd = [
        exe,
        url,
        "--dir",           dest_dir,
        "--out",           dest_name,
        # Multi-connection settings (国内加速核心)
        "--split",         str(connections),      # split into N pieces
        "--max-connection-per-server", str(connections),
        "--min-split-size", "1M",                 # 1 MB per shard minimum
        # Retry settings
        "--max-tries",     "10",
        "--retry-wait",    "5",
        # Timeout settings (适应 CN -> 海外高延迟)
        "--connect-timeout", "60",
        "--timeout",       str(timeout),
        # Continue / resume (断点续传)
        "--continue",      "true",
        # Quiet output (suppress progress bar — we use our own logging)
        "--quiet",         "true",
        # Disable aria2c RPC / daemon (just download and exit)
        "--enable-rpc",    "false",
        # User-Agent
        "--user-agent",    _BROWSER_UA,
    ]

    # If proxy is set, pass it to aria2c
    if PROXY:
        cmd += ["--all-proxy", PROXY]

    log.info(f"       [aria2c] {connections}-thread download: {dest_name}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout + 30,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            log.info(f"       [aria2c] OK: {dest.name} "
                     f"({dest.stat().st_size / 1024 / 1024:.1f} MB)")
            return True
        # aria2c error — log stderr for debugging
        if result.stderr:
            log.debug(f"       [aria2c] stderr: {result.stderr[:300]}")
        log.warning(f"       [aria2c] exit={result.returncode}, falling back to requests")
        return False
    except subprocess.TimeoutExpired:
        log.warning(f"       [aria2c] timeout after {timeout}s, falling back to requests")
        return False
    except Exception as e:
        log.debug(f"       [aria2c] error: {e}")
        return False


def _install_aria2c_windows() -> str:
    """
    Auto-install aria2c on Windows via winget or direct GitHub release download.
    Uses CN-accelerated mirror for the download itself.
    Returns path to aria2c.exe if successful, else empty string.
    """
    log.info("[aria2c] Not found. Attempting auto-install...")

    # Method 1: winget (Windows 10 1709+ built-in)
    winget = shutil.which("winget")
    if winget:
        try:
            log.info("[aria2c] Installing via winget ...")
            result = subprocess.run(
                [winget, "install", "--id", "aria2.aria2",
                 "--accept-package-agreements", "--accept-source-agreements",
                 "--silent"],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                found = shutil.which("aria2c")
                if found:
                    log.info(f"[aria2c] Installed via winget: {found}")
                    return found
        except Exception as e:
            log.debug(f"[aria2c] winget failed: {e}")

    # Method 2: direct download from GitHub releases (via CN mirror)
    # aria2 v1.37.0 win64 release
    aria2_url = (
        "https://github.com/aria2/aria2/releases/download/"
        "release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
    )
    aria2_url_accelerated = _cn_accelerate_url(aria2_url)

    script_dir = Path(__file__).parent
    zip_path   = script_dir / "aria2-win64.zip"

    try:
        log.info(f"[aria2c] Downloading from: {aria2_url_accelerated}")
        import zipfile
        import ssl
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(aria2_url_accelerated, context=ctx, timeout=120) as resp, \
             open(zip_path, "wb") as f:
            shutil.copyfileobj(resp, f)

        log.info("[aria2c] Extracting ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(str(script_dir))

        # Find aria2c.exe in extracted directory
        for root, _, files in os.walk(str(script_dir)):
            if "aria2c.exe" in files:
                src = Path(root) / "aria2c.exe"
                dst = script_dir / "aria2c.exe"
                if src != dst:
                    shutil.copy(str(src), str(dst))
                log.info(f"[aria2c] Ready: {dst}")
                return str(dst)
    except Exception as e:
        log.warning(f"[aria2c] Auto-install failed: {e}")
    finally:
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception:
                pass
    return ""


def download_file_resumable(url: str, dest: Path, timeout: int = 120,
                            max_retries: int = 5) -> bool:
    """
    Download with HTTP Range resume support.
    China network optimization: auto-retry on connection errors / stalls,
    using exponential back-off (1 s, 2 s, 4 s …).

    CN acceleration mode (--cn-accelerate):
      1. First tries aria2c with 16 parallel connections (国内多线程加速)
      2. Falls back to requests with Range resume if aria2c unavailable
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    # ── CN acceleration: try aria2c first (multi-thread,断点续传) ────────────
    if CN_ACCELERATE:
        accelerated_url = _cn_accelerate_url(url)
        if _aria2c_download(accelerated_url, dest, timeout=timeout):
            return True
        # aria2c failed or not installed — fall through to requests

    for attempt in range(1, max_retries + 1):
        existing_size = dest.stat().st_size if dest.exists() else 0
        headers = {"User-Agent": _UA}
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            if attempt == 1:
                log.info(f"       Resume from {existing_size/1024/1024:.1f} MB")
        try:
            if HAS_REQUESTS:
                r = _requests.get(url, headers=headers, timeout=timeout,
                                  proxies=_proxy_dict(), stream=True)
                if r.status_code == 416:
                    log.info("       File already fully downloaded")
                    return True
                if r.status_code == 200 and existing_size > 0:
                    existing_size = 0   # server ignored Range, restart
                elif r.status_code not in (200, 206):
                    log.warning(f"       HTTP {r.status_code}")
                    return False
                content_len = int(r.headers.get("Content-Length", 0))
                total       = content_len + existing_size
                mode        = "ab" if existing_size > 0 else "wb"
                downloaded  = existing_size
                with open(dest, mode) as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = downloaded / total * 100
                                with _print_lock:
                                    print(
                                        f"\r       {pct:5.1f}%  "
                                        f"{downloaded/1024/1024:.1f}/"
                                        f"{total/1024/1024:.1f} MB   ",
                                        end="", flush=True,
                                    )
                with _print_lock:
                    print()
                return True
            else:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    dest.write_bytes(resp.read())
                return True
        except Exception as e:
            wait = 2 ** (attempt - 1)
            if attempt < max_retries:
                log.warning(f"       Download error (attempt {attempt}/{max_retries}): {e} -- retry in {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"       Download failed after {max_retries} attempts: {e}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Quality detection
# ─────────────────────────────────────────────────────────────────────────────

def _read_flac_streaminfo(path: Path):
    """
    Parse FLAC STREAMINFO block to get (bit_depth, sample_rate).
    Returns (None, None) on failure.
    """
    try:
        with open(path, "rb") as f:
            sig = f.read(4)
            if sig != b"fLaC":
                return None, None
            while True:
                hdr = f.read(4)
                if len(hdr) < 4:
                    break
                block_type  = hdr[0] & 0x7F
                last_block  = (hdr[0] & 0x80) != 0
                block_len   = struct.unpack(">I", b"\x00" + hdr[1:4])[0]
                if block_type == 0:  # STREAMINFO
                    data = f.read(block_len)
                    # STREAMINFO layout (bytes):
                    # 2: min_block | 2: max_block | 3: min_frame | 3: max_frame
                    # 20 bits: sample_rate | 3 bits: channels-1 | 5 bits: bit_depth-1
                    # 36 bits: total_samples | 16 bytes: MD5
                    if len(data) < 18:
                        return None, None
                    sample_rate = (data[10] << 12) | (data[11] << 4) | (data[12] >> 4)
                    bit_depth   = ((data[12] & 0x01) << 4) | (data[13] >> 4) + 1
                    return bit_depth, sample_rate
                else:
                    f.seek(block_len, 1)
                if last_block:
                    break
    except Exception:
        pass
    return None, None


def _is_ost(name: str) -> bool:
    """Check if name (title/album/filename) contains OST keywords."""
    name_lower = name.lower()
    for kw in OST_KEYWORDS_EN:
        if kw in name_lower:
            return True
    for kw in OST_KEYWORDS_CN:
        if kw in name_lower:
            return True
    return False


def score_audio_file(path: Path, claimed_bitrate: int = 0) -> int:
    """
    Assign a quality score to a downloaded audio file.
    Higher = better quality. OST (soundtrack) versions get a large bonus
    so they always rank above non-OST files of similar quality.

    Policy: OST files accepted in ANY format; non-OST files only if FLAC.
    """
    ext = path.suffix.lower()
    is_ost = _is_ost(path.name) or _is_ost(str(path.parent.name))

    if ext == ".flac":
        bit_depth, sample_rate = _read_flac_streaminfo(path)
        base = QUALITY_FLAC16
        if bit_depth and sample_rate:
            if bit_depth >= 24 and sample_rate >= 176400:
                base = QUALITY_HIRESFLAC      # 24-bit >= 176.4kHz
            elif bit_depth >= 24 and sample_rate >= 88200:
                base = QUALITY_FLAC96         # 24-bit >= 88.2kHz
            elif bit_depth >= 24:
                base = QUALITY_FLAC48         # 24-bit 44.1/48kHz
            else:
                base = QUALITY_FLAC16             # 16-bit FLAC
        else:
            # Can't read metadata, assume 16-bit
            size_mb = path.stat().st_size / 1024 / 1024
            if size_mb > 50:
                base = QUALITY_FLAC96             # large file likely hi-res
            else:
                base = QUALITY_FLAC16
        if is_ost:
            return min(base + OST_BONUS, QUALITY_HIRESFLAC + OST_BONUS)
        return base

    if ext == ".wav":
        base = QUALITY_WAV
        if is_ost:
            return min(base + OST_BONUS, QUALITY_HIRESFLAC + OST_BONUS)
        return base
    if ext in (".alac", ".ape", ".wv", ".aif", ".aiff"):
        base = QUALITY_LOSSLESS
        if is_ost:
            return min(base + OST_BONUS, QUALITY_HIRESFLAC + OST_BONUS)
        return base
    # MP3 and other lossy formats — score WITH OST bonus
    # (OST files accepted in any format; non-OST lossy still rejected at filter)
    if ext in (".mp3",):
        base = QUALITY_MP3_320 if claimed_bitrate >= 320 else \
               QUALITY_MP3_HIGH if claimed_bitrate >= 192 else QUALITY_LOSSY
        if is_ost:
            return min(base + OST_BONUS, QUALITY_HIRESFLAC + OST_BONUS)
        return base
    if ext in (".aac", ".m4a"):
        base = QUALITY_AAC_HIGH if claimed_bitrate >= 256 else QUALITY_LOSSY
        if is_ost:
            return min(base + OST_BONUS, QUALITY_HIRESFLAC + OST_BONUS)
        return base
    if ext in (".ogg", ".opus"):
        base = QUALITY_LOSSY
        if is_ost:
            return min(base + OST_BONUS, QUALITY_HIRESFLAC + OST_BONUS)
        return base
    return QUALITY_UNKNOWN


def meets_target_quality(path: Path) -> bool:
    """Return True if file is 24-bit FLAC >= 192kHz."""
    ext = path.suffix.lower()
    if ext != ".flac":
        return False
    bit_depth, sample_rate = _read_flac_streaminfo(path)
    if bit_depth and sample_rate:
        return bit_depth >= 24 and sample_rate >= 192000
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Resume state
# ─────────────────────────────────────────────────────────────────────────────

def load_report(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {item["raw"]: item for item in data if "raw" in item}
    except Exception:
        return {}


def save_report(path: Path, report: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def get_existing_audio(song_dir: Path) -> Optional[Path]:
    """
    Scan song_dir for any existing audio file.
    Returns the best (highest quality score) existing file, or None.
    """
    if not song_dir.exists():
        return None
    candidates = [
        f for f in song_dir.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
        # Ignore temp/partial files
        and not f.name.startswith(".")
        and not f.name.endswith(".part")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda f: score_audio_file(f), reverse=True)
    return candidates[0]


def get_existing_lyrics(song_dir: Path) -> Optional[Path]:
    """Return existing .lrc file in song_dir, if any."""
    if not song_dir.exists():
        return None
    for f in song_dir.iterdir():
        if f.is_file() and f.suffix.lower() == ".lrc":
            return f
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Lyrics — 6-platform cascade (ordered by library size / coverage)
#
#  Priority  Platform          Coverage highlight
#  ────────  ────────────────  ──────────────────────────────────────────
#  1         lrclib.net        Largest open synced-LRC database (global)
#  2         NetEase (网易云)   Largest Chinese lyrics library
#  3         QQ Music (QQ音乐)  Second largest Chinese library
#  4         Musixmatch        Largest international lyrics database
#  5         Genius            English / rap / hip-hop specialty
#  6         megalobiz.com     LRC search engine, covers rare tracks
# ─────────────────────────────────────────────────────────────────────────────

import base64 as _base64


# ── Platform 1: lrclib.net ────────────────────────────────────────────────────
def _fetch_lrc_lrclib(title: str, artist: str) -> Optional[str]:
    url = LRCLIB_GET.format(
        artist=urllib.parse.quote(artist),
        title=urllib.parse.quote(title),
    )
    data = http_get_json(url)
    if data and data.get("syncedLyrics"):
        return data["syncedLyrics"]
    if data and data.get("plainLyrics"):
        return data["plainLyrics"]
    query = urllib.parse.quote(f"{title} {artist}")
    url2  = LRCLIB_SEARCH.format(query=query)
    results = http_get_json(url2)
    if isinstance(results, list) and results:
        best = results[0]
        return best.get("syncedLyrics") or best.get("plainLyrics")
    return None


# ── Platform 2: NetEase (网易云音乐) ──────────────────────────────────────────
def _fetch_lrc_netease(title: str, artist: str) -> Optional[str]:
    try:
        query = urllib.parse.quote(f"{title} {artist}")
        url   = NETEASE_SEARCH.format(query=query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://music.163.com/",
        }
        if HAS_REQUESTS:
            r = _requests.get(url, headers=headers, timeout=15,
                              proxies=_proxy_dict())
            r.raise_for_status()
            data = r.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        songs = (data.get("result") or {}).get("songs") or []
        if not songs:
            return None
        song_id = songs[0].get("id")
        if not song_id:
            return None
        lurl = NETEASE_LYRIC.format(id=song_id)
        if HAS_REQUESTS:
            r2 = _requests.get(lurl, headers=headers, timeout=15,
                               proxies=_proxy_dict())
            r2.raise_for_status()
            ldata = r2.json()
        else:
            req2 = urllib.request.Request(lurl, headers=headers)
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                ldata = json.loads(resp2.read().decode("utf-8"))
        lrc = ((ldata.get("lrc") or {}).get("lyric") or "").strip()
        return lrc if len(lrc) > 20 else None
    except Exception as e:
        log.debug(f"  [LRC/NetEase] {e}")
        return None


# ── Platform 3: QQ Music (QQ音乐) ────────────────────────────────────────────
def _fetch_lrc_qqmusic(title: str, artist: str) -> Optional[str]:
    try:
        query = urllib.parse.quote(f"{title} {artist}")
        url   = QQMUSIC_SEARCH.format(query=query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://y.qq.com/",
        }
        if HAS_REQUESTS:
            r = _requests.get(url, headers=headers, timeout=15,
                              proxies=_proxy_dict())
            r.raise_for_status()
            data = r.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        songs = (((data.get("data") or {}).get("song") or {}).get("list") or [])
        if not songs:
            return None
        mid = songs[0].get("songmid") or songs[0].get("mid")
        if not mid:
            return None
        lurl = QQMUSIC_LYRIC.format(mid=mid)
        headers2 = dict(headers)
        headers2["Referer"] = f"https://y.qq.com/n/yqq/song/{mid}.html"
        if HAS_REQUESTS:
            r2 = _requests.get(lurl, headers=headers2, timeout=15,
                               proxies=_proxy_dict())
            r2.raise_for_status()
            ldata = r2.json()
        else:
            req2 = urllib.request.Request(lurl, headers=headers2)
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                ldata = json.loads(resp2.read().decode("utf-8"))
        encoded = ldata.get("lyric") or ""
        if not encoded:
            return None
        lrc = _base64.b64decode(encoded).decode("utf-8", errors="replace").strip()
        return lrc if len(lrc) > 20 else None
    except Exception as e:
        log.debug(f"  [LRC/QQMusic] {e}")
        return None


# ── Platform 4: Musixmatch ────────────────────────────────────────────────────
def _fetch_lrc_musixmatch(title: str, artist: str) -> Optional[str]:
    try:
        url = MUSIXMATCH_SEARCH.format(
            title=urllib.parse.quote(title),
            artist=urllib.parse.quote(artist),
        )
        headers = {
            "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "authority":   "apic-desktop.musixmatch.com",
            "Cookie":      "x-mxm-token-guid=",
        }
        if HAS_REQUESTS:
            r = _requests.get(url, headers=headers, timeout=15,
                              proxies=_proxy_dict())
            r.raise_for_status()
            data = r.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        tracks = (((data.get("message") or {}).get("body") or {})
                  .get("track_list") or [])
        if not tracks:
            return None
        track_id = (tracks[0].get("track") or {}).get("track_id")
        if not track_id:
            return None
        lurl = (
            f"https://apic-desktop.musixmatch.com/ws/1.1/track.subtitle.get"
            f"?track_id={track_id}&subtitle_format=lrc"
            f"&usertoken=190523f77464fba06fa5f82a9bfbd5fb5f8fdbc4d8be9a13"
            f"&app_id=web-desktop-app-v1.0"
        )
        if HAS_REQUESTS:
            r2 = _requests.get(lurl, headers=headers, timeout=15,
                               proxies=_proxy_dict())
            r2.raise_for_status()
            ldata = r2.json()
        else:
            req2 = urllib.request.Request(lurl, headers=headers)
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                ldata = json.loads(resp2.read().decode("utf-8"))
        lrc = (((ldata.get("message") or {}).get("body") or {})
               .get("subtitle") or {}).get("subtitle_body") or ""
        return lrc.strip() if len(lrc.strip()) > 20 else None
    except Exception as e:
        log.debug(f"  [LRC/Musixmatch] {e}")
        return None


# ── Platform 5: Genius (plain lyrics, no timestamp) ──────────────────────────
def _fetch_lrc_genius(title: str, artist: str) -> Optional[str]:
    """
    Genius doesn't provide LRC timestamps, but returns plain lyrics as fallback.
    Uses the public search API (no API key needed for search metadata).
    """
    try:
        query = urllib.parse.quote(f"{title} {artist}")
        url   = f"https://genius.com/api/search/multi?per_page=1&q={query}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }
        if HAS_REQUESTS:
            r = _requests.get(url, headers=headers, timeout=15,
                              proxies=_proxy_dict())
            r.raise_for_status()
            data = r.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        sections = (data.get("response") or {}).get("sections") or []
        hit_url  = None
        for sec in sections:
            for hit in (sec.get("hits") or []):
                res = hit.get("result") or {}
                if res.get("type") == "song" or hit.get("type") == "song":
                    hit_url = res.get("url") or (hit.get("result") or {}).get("url")
                    if hit_url:
                        break
            if hit_url:
                break
        if not hit_url:
            return None
        # Scrape the lyrics page (plain text extraction)
        page_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if HAS_REQUESTS:
            rp = _requests.get(hit_url, headers=page_headers, timeout=20,
                               proxies=_proxy_dict())
            html = rp.text
        else:
            reqp = urllib.request.Request(hit_url, headers=page_headers)
            with urllib.request.urlopen(reqp, timeout=20) as respp:
                html = respp.read().decode("utf-8", errors="replace")
        # Extract text between data-lyrics-container divs
        chunks = re.findall(
            r'data-lyrics-container="true"[^>]*>(.*?)</div>',
            html, re.DOTALL
        )
        if not chunks:
            return None
        raw = " ".join(chunks)
        # Strip HTML tags
        plain = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
        plain = re.sub(r"<[^>]+>", "", plain).strip()
        # Unescape HTML entities
        plain = plain.replace("&#x27;", "'").replace("&amp;", "&").replace("&quot;", '"')
        return plain if len(plain) > 20 else None
    except Exception as e:
        log.debug(f"  [LRC/Genius] {e}")
        return None


# ── Platform 6: megalobiz.com (LRC search engine) ────────────────────────────
def _fetch_lrc_megalobiz(title: str, artist: str) -> Optional[str]:
    try:
        query = urllib.parse.quote(f"{title} {artist}")
        url   = MEGALOBIZ_SEARCH.format(query=query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if HAS_REQUESTS:
            r = _requests.get(url, headers=headers, timeout=20,
                              proxies=_proxy_dict())
            html = r.text
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        # Find first lrc detail link
        match = re.search(r'href="(/lrc/maker/[^"]+)"', html)
        if not match:
            return None
        detail_url = "https://www.megalobiz.com" + match.group(1)
        if HAS_REQUESTS:
            r2 = _requests.get(detail_url, headers=headers, timeout=20,
                               proxies=_proxy_dict())
            html2 = r2.text
        else:
            req2 = urllib.request.Request(detail_url, headers=headers)
            with urllib.request.urlopen(req2, timeout=20) as resp2:
                html2 = resp2.read().decode("utf-8", errors="replace")
        # Extract LRC content from <pre> or dedicated block
        m2 = re.search(r'<span[^>]*id="entity_box[^>]*>(.*?)</span>', html2, re.DOTALL)
        if not m2:
            m2 = re.search(r'<pre[^>]*>(.*?)</pre>', html2, re.DOTALL)
        if not m2:
            return None
        lrc = re.sub(r"<[^>]+>", "", m2.group(1)).strip()
        lrc = lrc.replace("&#x27;", "'").replace("&amp;", "&")
        return lrc if len(lrc) > 20 else None
    except Exception as e:
        log.debug(f"  [LRC/megalobiz] {e}")
        return None


# ── Multi-platform lyrics fetcher ─────────────────────────────────────────────
# Each entry: (platform_name, fetch_function)
# Order = priority (highest first, by lyrics library size)
_LYRICS_PLATFORMS = [
    ("lrclib",     _fetch_lrc_lrclib),
    ("NetEase",    _fetch_lrc_netease),
    ("QQMusic",    _fetch_lrc_qqmusic),
    ("Musixmatch", _fetch_lrc_musixmatch),
    ("Genius",     _fetch_lrc_genius),
    ("megalobiz",  _fetch_lrc_megalobiz),
]


def fetch_lyrics_multi(title: str, artist: str, primary_artist: str) -> Optional[str]:
    """
    Try all 6 lyrics platforms in priority order.
    Returns the first non-empty result found, or None.
    Tries both primary_artist and full artist string per platform.
    """
    tried_pairs: set = set()

    def _try(fn, t, a):
        key = (fn.__name__, t, a)
        if key in tried_pairs:
            return None
        tried_pairs.add(key)
        try:
            return fn(t, a)
        except Exception:
            return None

    for name, fn in _LYRICS_PLATFORMS:
        lrc = _try(fn, title, primary_artist)
        if not lrc and primary_artist != artist:
            lrc = _try(fn, title, artist)
        if lrc:
            log.info(f"  [LRC/{name}] Found lyrics for: {title}")
            return lrc
        log.debug(f"  [LRC/{name}] No lyrics for: {title}")
    return None


# keep old name for compatibility
def fetch_lyrics_lrclib(title: str, artist: str) -> Optional[str]:
    return _fetch_lrc_lrclib(title, artist)


def save_lyrics(song: dict, out_dir: Path) -> bool:
    lrc = fetch_lyrics_multi(song["title"], song["artist"], song["primary_artist"])
    if lrc:
        fname = safe_name(f"{song['title']} - {song['artist']}") + ".lrc"
        (out_dir / fname).write_text(lrc, encoding="utf-8")
        log.info(f"  [LRC] Saved: {fname}")
        return True
    log.warning(f"  [LRC] Not found on any platform: {song['title']}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1: Sockseek (Soulseek P2P) — HIGHEST PRIORITY
# https://github.com/fiso64/sockseek
# ─────────────────────────────────────────────────────────────────────────────

def _find_sockseek_exe() -> Optional[str]:
    if SOCKSEEK_EXE and Path(SOCKSEEK_EXE).exists():
        return SOCKSEEK_EXE
    script_dir = Path(__file__).parent
    for name in ("sockseek.exe", "sldl.exe", "sockseek", "sldl"):
        p = script_dir / name
        if p.exists():
            return str(p)
    for name in ("sockseek", "sldl"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _download_sockseek_if_needed() -> None:
    # Check if we already have it
    exe = _find_sockseek_exe()
    if exe:
        return
    log.info("[INFO] sockseek.exe not found. Attempting auto-download in Python...")
    import urllib.request
    import zipfile
    import ssl
    _sockseek_github_url = "https://github.com/fiso64/sockseek/releases/download/v3.0.3/sockseek-win-x64.zip"
    # Use CN-accelerated mirror when CN_ACCELERATE is enabled
    url = _cn_accelerate_url(_sockseek_github_url)
    if url != _sockseek_github_url:
        log.info(f"[CN] GitHub mirror: {url}")
    zip_path = Path("sockseek-win-x64.zip")
    try:
        log.info(f"Downloading from {url} ...")
        # Ensure modern TLS
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(url, context=ctx, timeout=60) as response, open(zip_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
            
        log.info("Extracting zip archive...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(".")
            
        if zip_path.exists():
            zip_path.unlink()
            
        # If extracted in a subdirectory, copy to root
        for root, dirs, files in os.walk("."):
            if "sockseek.exe" in files:
                target = Path(root) / "sockseek.exe"
                if root != ".":
                    shutil.copy(target, "sockseek.exe")
                    break
        log.info("[OK] sockseek.exe ready!")
    except Exception as e:
        log.warning(f"[WARN] Failed to auto-download sockseek: {e}. Fallback to non-Sockseek sources.")
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception:
                pass


def _write_sockseek_conf(conf_path: Path) -> None:
    conf_content = (
        "# Sockseek configuration (auto-generated)\n"
        "# https://github.com/fiso64/sockseek\n\n"
        f"username = {SOCKSEEK_USER}\n"
        f"password = {SOCKSEEK_PASS}\n\n"
        "# Prefer lossless hi-res first\n"
        "pref-format = flac,wav\n"
        f"pref-min-bitrate = {SOCKSEEK_MINBR}\n"
        "pref-max-bitrate = 9999\n\n"
        "# Accept all audio formats (OST files accepted in any format)\n"
        "# Non-OST non-FLAC files will be filtered out after download.\n"
        "format = flac,wav,alac,ape,wv,mp3,m4a,aac,ogg,opus\n\n"
        "# China network optimization\n"
        "fast-search = true\n"
        # connect-timeout: 60 s for China -> Soulseek server login
        "connect-timeout = 60000\n"
        "concurrent-jobs = 30\n"
        "concurrent-searches = 1\n"
        "search-timeout = 15000\n"
        "max-stale-time = 90000\n"
        "searches-per-time = 28\n"
        "searches-renew-time = 220\n"
        # max-retries=1: don't retry login within a process
        # (each retry = new login attempt → triggers server rate limiting)
        "max-retries = 1\n"
        "unknown-error-retries = 1\n"
        "fails-to-downrank = 1\n"
        "fails-to-ignore = 3\n"
        f"listen-port = {_get_free_port()}\n\n"
        "# Quality matching\n"
        "pref-strict-title = true\n"
        "pref-strict-album = true\n"
        "length-tol = 15\n"
        "pref-length-tol = 10\n\n"
        "shared-files = 0\n"
        "shared-folders = 0\n"
    )
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(conf_content, encoding="utf-8")
    log.info(f"  [SOCKSEEK] Config: {conf_path}")


def _build_sockseek_queries(song: dict) -> list:
    """
    Build a list of search query strings for Sockseek, from most specific
    to most broad.  Multiple variants improve hit rate for Chinese / mixed
    title songs.

    OST (soundtrack) variants are inserted FIRST for OST priority:
    - If title contains OST keywords, try "title OST" and "title soundtrack"
      before regular queries.
    """
    title  = song["title"]
    artist = song["primary_artist"]
    full_artist = song["artist"]

    # Strip parentheses/brackets and content inside them (e.g. "(叮咚鸡)")
    title_clean  = re.sub(r"[\(（【\[].+?[\)）】\]]", "", title).strip()
    artist_clean = re.sub(r"[\(（【\[].+?[\)）】\]]", "", artist).strip()

    queries = []
    seen = set()

    def _add(q):
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    # ── OST priority queries (inserted FIRST if title is OST) ───────────────
    if _is_ost(title):
        _add(f"{title} OST {artist}")
        _add(f"{title} OST")
        _add(f"{title} soundtrack {artist}")
        _add(f"{title} soundtrack")
        _add(f"{title_clean} OST {artist_clean}" if title_clean != title else "")
        log.info(f"  [OST] Detected OST title, adding OST-priority queries")

    # 1. Full "title artist"
    _add(f"{title} {artist}")
    # 2. Cleaned title + cleaned primary artist
    if title_clean != title or artist_clean != artist:
        _add(f"{title_clean} {artist_clean}")
    # 3. Title only (original)
    _add(title)
    # 4. Cleaned title only
    if title_clean != title:
        _add(title_clean)
    # 5. Title + full artist (all featuring artists)
    if full_artist != artist:
        _add(f"{title} {full_artist}")
    # 6. Replace Chinese middle-dot / special separators in artist
    alt_artist = re.sub(r"[·•/、，,].*", "", full_artist).strip()
    if alt_artist and alt_artist not in (artist, artist_clean):
        _add(f"{title} {alt_artist}")

    return queries


def sockseek_download(song: dict, out_dir: Path) -> Optional[Path]:
    """
    Download via Sockseek CLI (https://github.com/fiso64/sockseek).
    Uses a semaphore to serialize invocations (one at a time) so multiple
    threads don't conflict on the same Soulseek account login.

    Multiple query variants are tried in order; the first successful
    download wins.
    """
    exe = _find_sockseek_exe()
    if not exe:
        log.warning("  [SOCKSEEK] sockseek.exe not found. "
                    "Get it from: https://github.com/fiso64/sockseek/releases")
        return None
    if not SOCKSEEK_USER or not SOCKSEEK_PASS:
        log.warning("  [SOCKSEEK] No credentials configured")
        return None

    # Quick skip if Soulseek server was unreachable in previous attempts
    global _sockseek_offline, _sockseek_offline_since
    if _sockseek_offline:
        elapsed = time.time() - _sockseek_offline_since
        if elapsed < SOCKSEEK_OFFLINE_COOLDOWN:
            remaining = int(SOCKSEEK_OFFLINE_COOLDOWN - elapsed)
            log.info(f"  [SOCKSEEK] Soulseek server offline (detected {int(elapsed)}s ago, "
                     f"retry in {remaining}s) — skipping")
            return None
        else:
            # Cooldown expired — try again
            _sockseek_offline = False
            _sockseek_offline_since = 0.0
            log.info("  [SOCKSEEK] Cooldown expired, retrying Soulseek connection...")

    # TCP probe: quick check if Soulseek server is accepting connections
    # (3-8s vs 30-60s for sockseek.exe to timeout)
    if not _probe_soulseek_server():
        _sockseek_offline = True
        _sockseek_offline_since = time.time()
        log.info("  [SOCKSEEK] Soulseek 服务器拒绝连接（可能为登录频率限制），"
                 f"跳过该来源 (冷却 {SOCKSEEK_OFFLINE_COOLDOWN}s)")
        return None

    title     = song["title"]
    safe_base = safe_name(f"{title} - {song['artist']}")

    temp_out_dir = out_dir / f".sk_tmp_{safe_base[:60]}"
    temp_out_dir.mkdir(parents=True, exist_ok=True)

    conf_path = Path(exe).parent / "sockseek.conf"
    if SOCKSEEK_CONF:
        conf_path = Path(SOCKSEEK_CONF)
    if not conf_path.exists():
        _write_sockseek_conf(conf_path)

    queries = _build_sockseek_queries(song)
    log.info(f"  [SOCKSEEK] {len(queries)} query variants for: {title}")

    env = os.environ.copy()
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_SYSTEM_CONSOLE_ALLOW_ANSI_COLOR_REDIRECTION"] = "1"

    # Serialize Sockseek: only 1 process at a time per Soulseek account
    with _sockseek_sem:
        for qi, query in enumerate(queries, 1):
            # Early exit: if Soulseek server was detected as unreachable
            # in a previous query, skip remaining queries silently
            if _sockseek_offline:
                break

            # Inter-query delay: avoid rapid-fire logins that trigger
            # Soulseek server rate limiting (each query = new process + login)
            if qi > 1:
                time.sleep(5)

            log.info(f"  [SOCKSEEK] Query [{qi}/{len(queries)}]: {query!r}")

            # Clean temp dir before each attempt
            for _f in list(temp_out_dir.rglob("*")):
                if _f.is_file():
                    try:
                        _f.unlink()
                    except Exception:
                        pass

            # Pick a fresh random port for every subprocess call
            listen_port = _get_free_port()

            cmd = [
                exe,
                query,
                "--song",
                "--output-dir",      str(temp_out_dir),
                "--config",          str(conf_path),
                "--pref-format",     "flac,wav",
                f"--pref-min-bitrate={SOCKSEEK_MINBR}",
                "--fast-search",
                "--no-progress",
                "--name-format",     safe_base,
                "--no-write-index",
                "--max-retries",     "1",
                "--search-timeout",  "15000",
                # connect-timeout: use config file's 60000 (60s) — don't override via CLI
                "--listen-port",     str(listen_port),
            ]

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=str(Path(exe).parent),
                )
                _login_announced   = False
                _timeout_announced = False
                _conn_err_announced = False  # peer connection error (unobserved task exception)
                _conn_lost_count   = 0       # server connection lost retry counter
                _suppress_stack    = False   # suppress .NET stack trace lines
                _proc_killed       = False   # process killed due to server unreachable
                try:
                    for _line in proc.stdout:
                        _stripped = _line.rstrip("\r\n")
                        _low      = _stripped.lower()

                        # ── Mask lines that reveal the Soulseek username ──────
                        # Show "Logging in user account" instead of raw login output
                        if ("logging in as" in _low or
                                "soulseek" in _low and "login" in _low or
                                (SOCKSEEK_USER and SOCKSEEK_USER.lower() in _low and
                                 "login" in _low)):
                            if not _login_announced:
                                with _print_lock:
                                    print("  [SOCKSEEK] Logging in user account...")
                                _login_announced = True
                            continue

                        # ── Condense TimeoutException stack traces ────────────
                        # The error "System.TimeoutException: The wait timed out
                        # after 5000 milliseconds" prints a long .NET stacktrace.
                        # Replace it with a single user-friendly message.
                        if "system.timeoutexception" in _low or "timed out" in _low:
                            if not _timeout_announced:
                                with _print_lock:
                                    print("  [SOCKSEEK] Soulseek 服务器登录超时...")
                                _timeout_announced = True
                            # Mark Soulseek as offline so subsequent songs skip faster
                            _sockseek_offline = True
                            _sockseek_offline_since = time.time()
                            _suppress_stack = True
                            continue

                        # ── Proactively suppress .NET stack trace lines ──────
                        # Sometimes the stack trace "at Soulseek.SoulseekClient..."
                        # appears BEFORE the exception type line that sets
                        # _suppress_stack. Catch these proactively.
                        _s_check = _stripped.lstrip()
                        if _s_check.startswith("at ") and "Soulseek" in _s_check:
                            _suppress_stack = True
                            continue

                        # Suppress subsequent .NET stack trace lines
                        if _suppress_stack:
                            # Stack trace lines: "   at ...", "   ---", " ---> ..."
                            _s = _stripped.lstrip()
                            if (_s.startswith("at ") or
                                    _s.startswith("---") or
                                    _s.startswith("--->")):
                                continue
                            else:
                                _suppress_stack = False   # normal output resumes

                        # ── Suppress "Connection lost. Retrying" + stack trace ─
                        # When the Soulseek SERVER is unreachable, sockseek prints
                        # "Connection lost. Retrying..." followed by a .NET stack
                        # trace (at Soulseek.SoulseekClient.ConnectInternalAsync).
                        # After 3 failures, kill the process to avoid wasting time.
                        if "connection lost" in _low and "retrying" in _low:
                            _conn_lost_count += 1
                            if _conn_lost_count == 1:
                                with _print_lock:
                                    print("  [SOCKSEEK] Soulseek 服务器连接失败，正在重试...")
                            _sockseek_offline = True
                            _sockseek_offline_since = time.time()
                            _suppress_stack = True
                            if _conn_lost_count >= 3:
                                with _print_lock:
                                    print("  [SOCKSEEK] Soulseek 服务器不可达，跳过该来源...")
                                _proc_killed = True
                                proc.kill()
                                proc.wait()
                                break
                            continue

                        # ── Suppress "Unobserved task exception" / peer disconnect ─
                        # When a remote peer drops connection unexpectedly, the .NET
                        # Soulseek client logs a verbose multi-line stack trace
                        # (AggregateException → ConnectionException → SocketException).
                        # Condense it to a single user-friendly message.
                        # NOTE: this is a peer-level issue, NOT server offline.
                        if ("unobserved task exception" in _low or
                                "connectionreadexception" in _low or
                                ("failed to read" in _low and "bytes from" in _low) or
                                "远程主机强迫关闭" in _stripped):
                            if not _conn_err_announced:
                                with _print_lock:
                                    print("  [SOCKSEEK] 远程对端连接断开，尝试下一个来源...")
                                _conn_err_announced = True
                            _suppress_stack = True
                            continue

                        with _print_lock:
                            print(_stripped)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=SOCKSEEK_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    log.warning(f"  [SOCKSEEK] Timeout ({SOCKSEEK_TIMEOUT}s): {query!r}")
                    continue
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                log.warning(f"  [SOCKSEEK] Timeout ({SOCKSEEK_TIMEOUT}s): {query!r}")
                continue
            except FileNotFoundError:
                log.error(f"  [SOCKSEEK] Executable not found: {exe}")
                shutil.rmtree(temp_out_dir, ignore_errors=True)
                return None
            except Exception as e:
                log.warning(f"  [SOCKSEEK] Error: {e}")
                continue

            found_files = [
                f for f in temp_out_dir.rglob("*")
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS
            ]
            if not found_files:
                if not _proc_killed:
                    log.info(f"  [SOCKSEEK] No results for query [{qi}], trying next variant ...")
                continue

            # Sort: prefer FLAC/WAV, then largest size
            found_files.sort(key=lambda f: (
                0 if f.suffix.lower() == ".flac" else
                1 if f.suffix.lower() == ".wav"  else 2,
                -f.stat().st_size,
            ))
            best = found_files[0]

            dest = out_dir / (safe_base + best.suffix.lower())
            try:
                shutil.move(str(best), str(dest))
            except Exception as e:
                log.warning(f"  [SOCKSEEK] Move failed: {e}")
                dest = best

            log.info(f"  [SOCKSEEK] Got (query [{qi}]): {dest.name} "
                     f"({dest.stat().st_size/1024/1024:.1f} MB)")
            # Download succeeded — server is online, clear offline flag
            _sockseek_offline = False
            _sockseek_offline_since = 0.0
            shutil.rmtree(temp_out_dir, ignore_errors=True)
            return dest

    # Clean up sockseek temp dir and any leftover .probe_sockseek dirs
    shutil.rmtree(temp_out_dir, ignore_errors=True)
    probe_dir = out_dir / ".probe_sockseek"
    if probe_dir.exists():
        shutil.rmtree(probe_dir, ignore_errors=True)

    # Clean up any other leftover temp dirs in song_dir
    for d in out_dir.iterdir():
        if d.is_dir() and d.name.startswith(".sk_tmp_"):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    log.warning(f"  [SOCKSEEK] All {len(queries)} query variants failed: "
                f"{title} - {song['primary_artist']}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2: Internet Archive
# ─────────────────────────────────────────────────────────────────────────────

def archive_download(song: dict, out_dir: Path) -> Optional[Path]:
    query = urllib.parse.quote(f"{song['title']} {song['primary_artist']}")
    url   = ARCHIVE_SEARCH.format(query=query)
    data  = http_get_json(url)
    if not data:
        return None
    docs = data.get("response", {}).get("docs", [])
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        meta_url = f"https://archive.org/metadata/{identifier}/files"
        meta     = http_get_json(meta_url)
        if not meta:
            continue
        files = meta.get("result", [])
        # Prefer 24-bit FLAC files
        flac_files = [
            f for f in files
            if str(f.get("name", "")).lower().endswith(".flac")
        ]
        if not flac_files:
            continue
        # Pick largest FLAC (likely highest quality)
        flac_files.sort(key=lambda f: int(f.get("size", 0)), reverse=True)
        fname  = flac_files[0]["name"]
        dl_url = (
            f"https://archive.org/download/{identifier}/"
            f"{urllib.parse.quote(fname)}"
        )
        log.info(f"  [ARCHIVE] Found: {dl_url}")
        safe_base = safe_name(f"{song['title']} - {song['artist']}")
        dest      = out_dir / (safe_base + ".flac")
        if download_file_resumable(dl_url, dest):
            log.info(f"  [ARCHIVE] Downloaded: {dest.name}")
            return dest
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3: Free Music Archive (FMA)
# Note: FMA API key is public demo key; only works for free/open tracks
# ─────────────────────────────────────────────────────────────────────────────

def fma_download(song: dict, out_dir: Path) -> Optional[Path]:
    title  = urllib.parse.quote(song["title"])
    url    = FMA_SEARCH.format(title=title)
    data   = http_get_json(url)
    if not data:
        return None
    tracks = data.get("dataset", [])
    for track in tracks:
        # Check artist match (loose)
        track_artist = str(track.get("artist_name", "")).lower()
        song_artist  = song["primary_artist"].lower()
        track_title  = str(track.get("track_title", "")).lower()
        if song["title"].lower() not in track_title:
            continue
        # Get download URL for FLAC
        dl_url = track.get("track_url", "")
        if not dl_url:
            continue
        # FMA FLAC download URL pattern
        track_id = track.get("track_id")
        if track_id:
            flac_url = f"https://freemusicarchive.org/track/{track_id}/download?format=flac"
            safe_base = safe_name(f"{song['title']} - {song['artist']}")
            dest      = out_dir / (safe_base + ".flac")
            log.info(f"  [FMA] Trying: {flac_url}")
            if download_file_resumable(flac_url, dest):
                if dest.exists() and dest.stat().st_size > 1024 * 100:
                    log.info(f"  [FMA] Downloaded: {dest.name}")
                    return dest
                dest.unlink(missing_ok=True)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 4: Jamendo
# ─────────────────────────────────────────────────────────────────────────────

def jamendo_download(song: dict, out_dir: Path) -> Optional[Path]:
    title  = urllib.parse.quote(song["title"])
    artist = urllib.parse.quote(song["primary_artist"])
    url    = JAMENDO_SEARCH.format(title=title, artist=artist)
    data   = http_get_json(url)
    if not data:
        return None
    results = data.get("results", [])
    for track in results:
        # Prefer audio download URL with FLAC
        audio_url = track.get("audiodownload") or track.get("audio")
        if not audio_url:
            continue
        # Try to get FLAC via audiodownload_allowed
        if not track.get("audiodownload_allowed", False):
            continue
        # Use FLAC download endpoint
        track_id  = track.get("id")
        flac_url  = (
            f"https://api.jamendo.com/v3.0/tracks/file/"
            f"?client_id=b6747d04&id={track_id}&audioformat=flac"
        )
        safe_base = safe_name(f"{song['title']} - {song['artist']}")
        dest      = out_dir / (safe_base + ".flac")
        log.info(f"  [JAMENDO] Trying: {flac_url}")
        if download_file_resumable(flac_url, dest):
            if dest.exists() and dest.stat().st_size > 1024 * 100:
                log.info(f"  [JAMENDO] Downloaded: {dest.name}")
                return dest
            dest.unlink(missing_ok=True)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 5: YouTube via yt-dlp (best audio -> FLAC)
# ─────────────────────────────────────────────────────────────────────────────

def ytdlp_download(song: dict, out_dir: Path, search_prefix: str = "ytsearch1") -> Optional[Path]:
    if not HAS_YTDLP:
        log.warning("  [YTDLP] yt-dlp not installed")
        return None

    # OST priority: if title is OST, try OST search first
    title = song['title']
    artist = song['primary_artist']
    if _is_ost(title):
        search_query  = f"{title} OST {artist} official audio"
    else:
        search_query  = f"{title} {artist} official audio"
    safe_base     = safe_name(f"{title} - {song['artist']}")
    out_template  = str(out_dir / f"{safe_base}.%(ext)s")

    ydl_opts = {
        "format":           "bestaudio[ext=flac]/bestaudio[acodec=flac]/bestaudio",
        "outtmpl":          out_template,
        "noplaylist":       True,
        "quiet":            False,
        "no_warnings":      False,
        "default_search":   search_prefix,
        "continuedl":       True,
        "noprogress":       False,
        # ── China network optimization ──────────────────────────────────────
        "retries":              10,          # more retries for unstable CN links
        "fragment_retries":     10,
        "file_access_retries":  5,
        "socket_timeout":       60,          # 60 s — higher CN -> global latency
        "http_chunk_size":      1024 * 1024, # 1 MB chunks: better for high-latency
        # CN acceleration: 16 parallel fragment downloads (充分利用国内带宽)
        "concurrent_fragment_downloads": 16 if CN_ACCELERATE else 4,
        # Prefer FLAC and higher bitrate when multiple formats available
        "format_sort":          ["ext:flac", "acodec:flac", "abr", "asr"],
        "postprocessors": [
            {
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "flac",
                "preferredquality": "0",
            }
        ],
        "addmetadata": True,
    }
    if PROXY:
        ydl_opts["proxy"] = PROXY
    # Provide ffmpeg location for audio conversion
    ffmpeg_exe = _find_ffmpeg()
    if ffmpeg_exe:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_exe).parent)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"{search_prefix}:{search_query}", download=True)
            if info and "entries" in info:
                info = info["entries"][0] if info["entries"] else None
            if info:
                # Only accept FLAC (postprocessor converts to FLAC)
                for ext in ("flac",):
                    candidate = out_dir / f"{safe_base}.{ext}"
                    if candidate.exists():
                        log.info(f"  [YTDLP] Downloaded ({ext}): {candidate.name}")
                        return candidate
        # ── Reject video files ────────────────────────────────────────
        #    yt-dlp might download a video format (.mp4/.webm/.mkv etc.)
        #    even with bestaudio (rare edge case). Delete and skip.
        _cleanup_video_in_dir(out_dir, safe_base)
    except Exception as e:
        err_msg = str(e)
        log.warning(f"  [YTDLP] Error: {err_msg}")
        # ── Cleanup any video files left from failed download ──────────
        _cleanup_video_in_dir(out_dir, safe_base)
        # Retry with audio-only fallback if "format not available" error
        if "format is not available" in err_msg.lower() or "requested format" in err_msg.lower():
            log.info(f"  [YTDLP] Retrying with audio-only fallback format...")
            fallback_opts = dict(ydl_opts)
            fallback_opts["format"] = "bestaudio"
            # Remove format_sort for fallback
            fallback_opts.pop("format_sort", None)
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(f"{search_prefix}:{search_query}", download=True)
                    if info and "entries" in info:
                        info = info["entries"][0] if info["entries"] else None
                    if info:
                        for ext in ("flac",):
                            candidate = out_dir / f"{safe_base}.{ext}"
                            if candidate.exists():
                                log.info(f"  [YTDLP] Downloaded ({ext}, fallback): {candidate.name}")
                                return candidate
                # ── Reject video files from fallback ────────────────────
                _cleanup_video_in_dir(out_dir, safe_base)
            except Exception as e2:
                log.warning(f"  [YTDLP] Audio-only fallback also failed: {e2}")
                _cleanup_video_in_dir(out_dir, safe_base)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 6: Bandcamp via yt-dlp
# ─────────────────────────────────────────────────────────────────────────────

def bandcamp_download(song: dict, out_dir: Path) -> Optional[Path]:
    """
    Search Bandcamp via yt-dlp bandcamp:search.
    Bandcamp offers FLAC downloads for free/name-your-price releases.
    """
    if not HAS_YTDLP:
        return None

    search_query  = f"{song['title']} {song['primary_artist']}"
    safe_base     = safe_name(f"{song['title']} - {song['artist']}")
    out_template  = str(out_dir / f"{safe_base}_bc.%(ext)s")

    ydl_opts = {
        "format":           "bestaudio[ext=flac]/bestaudio",
        "outtmpl":          out_template,
        "noplaylist":       True,
        "quiet":            True,
        "no_warnings":      True,
        "default_search":   "bcsearch1",
        "continuedl":       True,
        # ── China network optimization ──────────────────────────────────────
        "retries":              8,
        "fragment_retries":     8,
        "socket_timeout":       60,
        "http_chunk_size":      1024 * 1024,
        "concurrent_fragment_downloads": 16 if CN_ACCELERATE else 4,
        "js_runtimes":      {"node": None},
        # Prefer FLAC and higher bitrate
        "format_sort":          ["ext:flac", "acodec:flac", "abr", "asr"],
        "postprocessors": [
            {
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "flac",
                "preferredquality": "0",
            }
        ],
    }
    if PROXY:
        ydl_opts["proxy"] = PROXY
    # Provide ffmpeg location for audio conversion
    ffmpeg_exe = _find_ffmpeg()
    if ffmpeg_exe:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_exe).parent)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"bcsearch1:{search_query}", download=True)
            if info and "entries" in info:
                info = info["entries"][0] if info["entries"] else None
            if info:
                # Only accept FLAC (postprocessor converts to FLAC)
                for ext in ("flac",):
                    candidate = out_dir / f"{safe_base}_bc.{ext}"
                    if candidate.exists():
                        log.info(f"  [BANDCAMP] Downloaded ({ext}): {candidate.name}")
                        return candidate
        # ── Reject video files ────────────────────────────────────────
        _cleanup_video_in_dir(out_dir, f"{safe_base}_bc")
    except Exception as e:
        err_msg = str(e)
        log.debug(f"  [BANDCAMP] Error: {err_msg}")
        _cleanup_video_in_dir(out_dir, f"{safe_base}_bc")
        # Retry with audio-only fallback if "format not available" error
        if "format is not available" in err_msg.lower() or "requested format" in err_msg.lower():
            log.info(f"  [BANDCAMP] Retrying with audio-only fallback format...")
            fallback_opts = dict(ydl_opts)
            fallback_opts["format"] = "bestaudio"
            fallback_opts.pop("format_sort", None)
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(f"bcsearch1:{search_query}", download=True)
                    if info and "entries" in info:
                        info = info["entries"][0] if info["entries"] else None
                    if info:
                        for ext in ("flac",):
                            candidate = out_dir / f"{safe_base}_bc.{ext}"
                            if candidate.exists():
                                log.info(f"  [BANDCAMP] Downloaded ({ext}, fallback): {candidate.name}")
                                return candidate
                _cleanup_video_in_dir(out_dir, f"{safe_base}_bc")
            except Exception as e2:
                log.debug(f"  [BANDCAMP] Audio-only fallback also failed: {e2}")
                _cleanup_video_in_dir(out_dir, f"{safe_base}_bc")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 7: NetEase Cloud Music (网易云音乐) — audio download + FLAC conversion
# Uses cloudsearch API (POST) for search, outer URL for audio, ffmpeg for FLAC
# ─────────────────────────────────────────────────────────────────────────────

def netease_download(song: dict, out_dir: Path) -> Optional[Path]:
    """Download from NetEase Cloud Music; convert to FLAC via ffmpeg."""
    if not HAS_REQUESTS:
        return None

    title  = song["title"]
    artist = song["primary_artist"]
    safe_base = safe_name(f"{title} - {artist}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://music.163.com/",
    }

    try:
        # Step 1: Search using cloudsearch API (POST — confirmed working)
        # OST priority: if title is OST, try OST-related queries first
        search_queries = [f"{title} {artist}", title]
        if _is_ost(title):
            search_queries = [f"{title} OST {artist}", f"{title} OST",
                              f"{title} soundtrack {artist}"] + search_queries

        songs = []
        for sq in search_queries:
            try:
                r = _requests.post(
                    "https://music.163.com/api/cloudsearch/pc",
                    data={"s": sq, "type": "1", "offset": "0",
                          "total": "true", "limit": "5"},
                    headers=headers, timeout=15, proxies=_proxy_dict()
                )
                r.raise_for_status()
                found = (r.json().get("result") or {}).get("songs") or []
                if found:
                    songs = found
                    break
            except Exception:
                continue

        if not songs:
            log.info(f"  [NETEASE] No results for: {title} - {artist}")
            return None

        # Step 2: Try each result
        for s in songs[:3]:
            song_id = s.get("id")
            if not song_id:
                continue

            # 2a: Try song/url/v1 for FLAC (needs auth, often fails)
            for url_api in [
                f"https://music.163.com/api/song/url/v1?id={song_id}&level=lossless",
                f"https://music.163.com/api/song/enhance/player/url?id={song_id}&br=999000",
            ]:
                try:
                    r2 = _requests.post(url_api, headers=headers, timeout=15,
                                        proxies=_proxy_dict())
                    if r2.status_code != 200:
                        continue
                    url_data = r2.json().get("data") or []
                    if not url_data:
                        continue
                    d = url_data[0]
                    dl_url = d.get("url")
                    if not dl_url:
                        continue
                    br = d.get("br", 0)
                    ext = (d.get("type") or "").lower()

                    # Download the audio file
                    out_dir.mkdir(parents=True, exist_ok=True)
                    raw_ext = "flac" if ext == "flac" or br >= 800000 else "mp3"
                    raw_path = out_dir / f"{safe_base}_ne_raw.{raw_ext}"
                    flac_path = out_dir / f"{safe_base}_ne.flac"

                    r3 = _requests.get(dl_url, headers=headers, timeout=120,
                                       stream=True, proxies=_proxy_dict())
                    r3.raise_for_status()
                    with open(raw_path, "wb") as f:
                        for chunk in r3.iter_content(CHUNK_SIZE):
                            f.write(chunk)

                    if raw_path.exists() and raw_path.stat().st_size > 1024 * 50:
                        if raw_ext == "flac":
                            # Already FLAC — rename and return
                            raw_path.rename(flac_path)
                            log.info(f"  [NETEASE] Downloaded FLAC: {flac_path.name} "
                                     f"({flac_path.stat().st_size/1024/1024:.1f} MB)")
                            return flac_path
                        else:
                            # Convert to FLAC
                            if _convert_to_flac(raw_path, flac_path):
                                log.info(f"  [NETEASE] Downloaded + converted to FLAC: "
                                         f"{flac_path.name} ({flac_path.stat().st_size/1024/1024:.1f} MB)")
                                return flac_path
                    raw_path.unlink(missing_ok=True)
                    flac_path.unlink(missing_ok=True)
                except Exception:
                    continue

            # 2b: Try outer URL (redirects to audio file, usually MP3)
            try:
                outer = f"https://music.163.com/song/media/outer/url?id={song_id}"
                r4 = _requests.get(outer, headers=headers, timeout=15,
                                   proxies=_proxy_dict(), allow_redirects=False)
                if r4.status_code == 302:
                    loc = r4.headers.get("Location", "")
                    if loc and "http" in loc:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        raw_path = out_dir / f"{safe_base}_ne_raw.mp3"
                        flac_path = out_dir / f"{safe_base}_ne.flac"

                        r5 = _requests.get(loc, headers=headers, timeout=120,
                                           stream=True, proxies=_proxy_dict())
                        if r5.status_code == 200:
                            with open(raw_path, "wb") as f:
                                for chunk in r5.iter_content(CHUNK_SIZE):
                                    f.write(chunk)
                            if raw_path.exists() and raw_path.stat().st_size > 1024 * 50:
                                if _convert_to_flac(raw_path, flac_path):
                                    log.info(f"  [NETEASE] Outer URL → FLAC: "
                                             f"{flac_path.name} ({flac_path.stat().st_size/1024/1024:.1f} MB)")
                                    return flac_path
                        raw_path.unlink(missing_ok=True)
                        flac_path.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as e:
        log.debug(f"  [NETEASE] Error: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 8: QQ Music (QQ音乐) — smartbox search + vkey download + FLAC convert
# ─────────────────────────────────────────────────────────────────────────────

def qqmusic_download(song: dict, out_dir: Path) -> Optional[Path]:
    """Download from QQ Music; convert to FLAC via ffmpeg."""
    if not HAS_REQUESTS:
        return None

    title  = song["title"]
    artist = song["primary_artist"]
    safe_base = safe_name(f"{title} - {artist}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://y.qq.com/",
    }

    try:
        # Step 1: Search using smartbox (confirmed working)
        # OST priority: try OST queries first if title is OST
        import random as _rng
        search_keys = [f"{title} {artist}", title]
        if _is_ost(title):
            search_keys = [f"{title} OST {artist}", f"{title} OST",
                           f"{title} soundtrack"] + search_keys

        items = []
        for sk in search_keys:
            query_enc = urllib.parse.quote(sk)
            smartbox_url = f"https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?key={query_enc}&format=json"
            try:
                r = _requests.get(smartbox_url, headers=headers, timeout=15,
                                  proxies=_proxy_dict())
                if r.status_code == 200:
                    found_items = r.json().get("data", {}).get("song", {}).get("itemlist", [])
                    if found_items:
                        items = found_items
                        break
            except Exception:
                continue

        if not items:
            log.info(f"  [QQMUSIC] No results for: {title} - {artist}")
            return None

        # Step 2: Try each result
        guid = str(_rng.randint(1000000000, 9999999999))

        for item in items[:3]:
            mid = item.get("mid", "")
            if not mid:
                continue

            # Get vkey for download
            vkey_payload = json.dumps({
                "req_0": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "guid": guid, "songmid": [mid], "songtype": [0],
                        "uin": "0", "loginflag": 1, "platform": "20"
                    }
                },
                "comm": {"uin": 0, "format": "json", "ct": 24, "cv": 0}
            })
            vkey_url = "https://u.y.qq.com/cgi-bin/musicu.fcg?data=" + \
                       urllib.parse.quote(vkey_payload)

            try:
                r2 = _requests.get(vkey_url, headers=headers, timeout=15,
                                   proxies=_proxy_dict())
                r2.raise_for_status()
                vdata = r2.json()
                midurlinfo = vdata.get("req_0", {}).get("data", {}).get("midurlinfo") or []
                sip = vdata.get("req_0", {}).get("data", {}).get("sip") or []

                if not midurlinfo or not sip:
                    continue

                info = midurlinfo[0]
                vkey = info.get("vkey", "")
                filename = info.get("filename", "")
                purl = info.get("purl", "")

                if not vkey and not purl:
                    continue

                out_dir.mkdir(parents=True, exist_ok=True)
                flac_path = out_dir / f"{safe_base}_qq.flac"

                # Try FLAC first (F000 prefix), then MP3 320 (M500), then M4A (C400)
                for prefix, fmt_ext, fmt_label in [
                    ("F000", "flac", "FLAC"),
                    ("M500", "mp3", "MP3-320"),
                    ("C400", "m4a", "M4A-128"),
                ]:
                    if filename.startswith(("M500", "C400", "F000")):
                        test_fn = prefix + filename[4:]
                    else:
                        test_fn = f"{prefix}{mid}.{fmt_ext}"

                    if purl:
                        dl_url = f"{sip[0]}{purl}"
                    else:
                        dl_url = f"{sip[0]}{test_fn}?guid={guid}&vkey={vkey}&fromtag=46&uin=0"

                    raw_path = out_dir / f"{safe_base}_qq_raw.{fmt_ext}"

                    try:
                        r3 = _requests.get(dl_url, headers=headers, timeout=120,
                                           stream=True, proxies=_proxy_dict())
                        if r3.status_code != 200:
                            continue
                        with open(raw_path, "wb") as f:
                            for chunk in r3.iter_content(CHUNK_SIZE):
                                f.write(chunk)

                        if raw_path.exists() and raw_path.stat().st_size > 1024 * 50:
                            if fmt_ext == "flac":
                                raw_path.rename(flac_path)
                                log.info(f"  [QQMUSIC] Downloaded FLAC: {flac_path.name} "
                                         f"({flac_path.stat().st_size/1024/1024:.1f} MB)")
                                return flac_path
                            else:
                                if _convert_to_flac(raw_path, flac_path):
                                    log.info(f"  [QQMUSIC] {fmt_label} → FLAC: "
                                             f"{flac_path.name} ({flac_path.stat().st_size/1024/1024:.1f} MB)")
                                    return flac_path
                        raw_path.unlink(missing_ok=True)
                        flac_path.unlink(missing_ok=True)
                    except Exception:
                        raw_path.unlink(missing_ok=True)
                        continue

            except Exception as e:
                log.debug(f"  [QQMUSIC] Error for mid={mid}: {e}")
                continue

    except Exception as e:
        log.debug(f"  [QQMUSIC] Error: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 9: Kugou Music (酷狗音乐) — mobile API + FLAC conversion
# Uses songsearch for search, m.kugou.com mobile API for audio URL
# ─────────────────────────────────────────────────────────────────────────────

def kugou_download(song: dict, out_dir: Path) -> Optional[Path]:
    """Download from Kugou Music; convert to FLAC via ffmpeg."""
    if not HAS_REQUESTS:
        return None

    title  = song["title"]
    artist = song["primary_artist"]
    query  = urllib.parse.quote(f"{title} {artist}")
    safe_base = safe_name(f"{title} - {artist}")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.kugou.com/",
    }
    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) "
                       "AppleWebKit/603.1.30 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1",
    }

    try:
        # Step 1: Search using songsearch (confirmed working)
        # OST priority: try OST queries first if title is OST
        search_keys = [f"{title} {artist}", title]
        if _is_ost(title):
            search_keys = [f"{title} OST {artist}", f"{title} OST",
                           f"{title} soundtrack"] + search_keys

        songs = []
        for sk in search_keys:
            query = urllib.parse.quote(sk)
            search_url = f"https://songsearch.kugou.com/song_search_v2?keyword={query}&page=1&pagesize=5"
            try:
                r = _requests.get(search_url, headers=headers, timeout=15,
                                  proxies=_proxy_dict(), verify=False)
                if r.status_code == 200:
                    found = (r.json().get("data") or {}).get("lists") or []
                    if found:
                        songs = found
                        break
            except Exception:
                continue

        if not songs:
            log.info(f"  [KUGOU] No results for: {title} - {artist}")
            return None

        # Step 2: Try each result via mobile API
        for s in songs[:3]:
            file_hash = s.get("FileHash") or s.get("filehash") or ""
            if not file_hash:
                continue

            # Try SQ hash first (if available)
            sq_hash = s.get("SQHash") or s.get("sqhash") or ""
            hq_hash = s.get("HQHash") or s.get("hqhash") or ""

            # Try each hash: SQ → FileHash
            for h, label in [(sq_hash, "SQ"), (file_hash, "STD")]:
                if not h:
                    continue

                murl = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={h}"
                try:
                    r2 = _requests.get(murl, headers=mobile_headers, timeout=15,
                                       proxies=_proxy_dict(), verify=False)
                    if r2.status_code != 200:
                        continue
                    mdata = r2.json()
                    play_url = mdata.get("url", "")
                    ext_name = (mdata.get("extName") or "").lower()
                    bit_rate = mdata.get("bitRate", 0)

                    if not play_url:
                        continue

                    out_dir.mkdir(parents=True, exist_ok=True)
                    flac_path = out_dir / f"{safe_base}_kg.flac"

                    # If already FLAC
                    if ext_name == "flac" or "flac" in play_url.lower():
                        raw_path = out_dir / f"{safe_base}_kg_raw.flac"
                        r3 = _requests.get(play_url, headers=headers, timeout=120,
                                           stream=True, proxies=_proxy_dict(), verify=False)
                        if r3.status_code == 200:
                            with open(raw_path, "wb") as f:
                                for chunk in r3.iter_content(CHUNK_SIZE):
                                    f.write(chunk)
                            if raw_path.exists() and raw_path.stat().st_size > 1024 * 50:
                                raw_path.rename(flac_path)
                                log.info(f"  [KUGOU] Downloaded FLAC ({label}): {flac_path.name} "
                                         f"({flac_path.stat().st_size/1024/1024:.1f} MB)")
                                return flac_path
                        raw_path.unlink(missing_ok=True)
                    else:
                        # Download audio (MP3) and convert to FLAC
                        raw_ext = ext_name if ext_name in ("mp3", "m4a", "aac") else "mp3"
                        raw_path = out_dir / f"{safe_base}_kg_raw.{raw_ext}"
                        r3 = _requests.get(play_url, headers=headers, timeout=120,
                                           stream=True, proxies=_proxy_dict(), verify=False)
                        if r3.status_code == 200:
                            with open(raw_path, "wb") as f:
                                for chunk in r3.iter_content(CHUNK_SIZE):
                                    f.write(chunk)
                            if raw_path.exists() and raw_path.stat().st_size > 1024 * 50:
                                if _convert_to_flac(raw_path, flac_path):
                                    log.info(f"  [KUGOU] {label} ({bit_rate}kbps) → FLAC: "
                                             f"{flac_path.name} ({flac_path.stat().st_size/1024/1024:.1f} MB)")
                                    return flac_path
                        raw_path.unlink(missing_ok=True)
                        flac_path.unlink(missing_ok=True)
                except Exception as e:
                    log.debug(f"  [KUGOU] Error for hash={h[:12]}: {e}")
                    continue

    except Exception as e:
        log.debug(f"  [KUGOU] Error: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 10: Migu Music (咪咕音乐) — search + audio download + FLAC convert
# ─────────────────────────────────────────────────────────────────────────────

def migu_download(song: dict, out_dir: Path) -> Optional[Path]:
    """Download from Migu Music; convert to FLAC via ffmpeg."""
    if not HAS_REQUESTS:
        return None

    title  = song["title"]
    artist = song["primary_artist"]
    query  = urllib.parse.quote(f"{title} {artist}")
    safe_base = safe_name(f"{title} - {artist}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
        "Referer":    "https://music.migu.cn/",
        "channel":    "0146921",
    }

    try:
        # Step 1: Search using Migu mobile API
        # OST priority: try OST queries first if title is OST
        search_keys = [f"{title} {artist}", title]
        if _is_ost(title):
            search_keys = [f"{title} OST {artist}", f"{title} OST",
                           f"{title} soundtrack"] + search_keys

        musics = []
        for sk in search_keys:
            query = urllib.parse.quote(sk)
            search_url = (
                f"https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do?"
                f"ua=Android_migu&version=5.0.1&text={query}&pageNo=1&pageSize=5"
                f"&searchSwitch=%7B%22song%22%3A1%7D"
            )
            try:
                r = _requests.get(search_url, headers=headers, timeout=15,
                                  proxies=_proxy_dict(), verify=False)
                if r.status_code == 200:
                    mdata = r.json()
                    found = (mdata.get("songResultData") or {}).get("result") or []
                    if not found:
                        found = mdata.get("songList") or []
                    if found:
                        musics = found
                        break
            except Exception:
                continue

        if not musics:
            log.info(f"  [MIGU] No results for: {title} - {artist}")
            return None

        # Step 2: Try each result for download URL
        for m in musics[:3]:
            copyright_id = m.get("copyrightId") or ""
            song_id = m.get("id") or m.get("songId") or ""
            if not copyright_id and not song_id:
                continue

            cid = copyright_id or song_id

            # Try to find audio URL in the search result itself
            dl_url = ""
            # Check newRateFormats in search result
            nrf = m.get("newRateFormats") or []
            if isinstance(nrf, list):
                for fmt in nrf:
                    if isinstance(fmt, dict):
                        url_val = fmt.get("url") or ""
                        fmt_type = fmt.get("formatType", "")
                        if url_val:
                            # Prefer FLAC/SQ/ZQ, but accept any
                            dl_url = url_val
                            if fmt_type in ("FLAC", "SQ", "ZQ"):
                                break

            # If no URL in search, try detail API
            if not dl_url:
                detail_urls = [
                    f"https://music.migu.cn/v3/api/music/audioPlayer/songs?copyrightId={cid}",
                    f"https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/content/song.do?"
                    f"copyrightId={cid}&resourceType=2",
                ]
                for durl in detail_urls:
                    try:
                        r2 = _requests.get(durl, headers=headers, timeout=15,
                                           proxies=_proxy_dict(), verify=False)
                        if r2.status_code != 200:
                            continue
                        sdata = r2.json()
                        items = sdata if isinstance(sdata, list) else [sdata]
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            dl_url = (item.get("flacUrl") or item.get("playUrl")
                                      or item.get("url") or "")
                            if not dl_url:
                                nrf2 = item.get("newRateFormats") or {}
                                if isinstance(nrf2, dict):
                                    dl_url = nrf2.get("flac") or nrf2.get("sq") or ""
                                elif isinstance(nrf2, list):
                                    for fmt in nrf2:
                                        if isinstance(fmt, dict) and fmt.get("url"):
                                            dl_url = fmt["url"]
                                            break
                            if dl_url:
                                break
                        if dl_url:
                            break
                    except Exception:
                        continue

            if not dl_url:
                continue

            # Download and convert to FLAC
            out_dir.mkdir(parents=True, exist_ok=True)
            flac_path = out_dir / f"{safe_base}_mg.flac"

            # Determine if URL is already FLAC
            is_flac = "flac" in dl_url.lower()
            raw_ext = "flac" if is_flac else "mp3"
            raw_path = out_dir / f"{safe_base}_mg_raw.{raw_ext}"

            try:
                r3 = _requests.get(dl_url, headers=headers, timeout=120,
                                   stream=True, proxies=_proxy_dict(), verify=False)
                if r3.status_code != 200:
                    continue
                with open(raw_path, "wb") as f:
                    for chunk in r3.iter_content(CHUNK_SIZE):
                        f.write(chunk)

                if raw_path.exists() and raw_path.stat().st_size > 1024 * 50:
                    if is_flac:
                        raw_path.rename(flac_path)
                        log.info(f"  [MIGU] Downloaded FLAC: {flac_path.name} "
                                 f"({flac_path.stat().st_size/1024/1024:.1f} MB)")
                        return flac_path
                    else:
                        if _convert_to_flac(raw_path, flac_path):
                            log.info(f"  [MIGU] Converted to FLAC: {flac_path.name} "
                                     f"({flac_path.stat().st_size/1024/1024:.1f} MB)")
                            return flac_path
                raw_path.unlink(missing_ok=True)
                flac_path.unlink(missing_ok=True)
            except Exception:
                raw_path.unlink(missing_ok=True)
                flac_path.unlink(missing_ok=True)
                continue

    except Exception as e:
        log.debug(f"  [MIGU] Error: {e}")
    return None


class SourceResult:
    """Holds result from one source attempt."""
    __slots__ = ("source_name", "path", "quality_score")

    def __init__(self, source_name: str, path: Optional[Path]):
        self.source_name   = source_name
        self.path          = path
        self.quality_score = score_audio_file(path) if path and path.exists() else 0


def _try_all_sources(song: dict, song_dir: Path,
                     disable_sockseek: bool = False,
                     existing_quality: int = 0) -> list:
    """
    Try all 10 sources, collect SourceResults.

    existing_quality: quality score of an already-downloaded file in song_dir.
      - Sources are tried in order.
      - A newly downloaded file is only kept if its score > existing_quality
        AND it is either an OST file (any format) or a FLAC file.
        Non-OST non-FLAC files (MP3, AAC, OGG, etc.) are always rejected.
        If it's not better, it is deleted immediately and skipped.
      - If existing_quality >= QUALITY_HIRESFLAC + OST_BONUS (max target),
        all sources are skipped entirely and an empty list is returned.
      - Returns list of SourceResults that are strictly better than existing.
    """
    results = []

    # Already at maximum quality target (including OST bonus) — nothing to improve
    # The max possible score is QUALITY_HIRESFLAC + OST_BONUS = 130
    if existing_quality >= QUALITY_HIRESFLAC + OST_BONUS:
        log.info("  [SKIP ALL] Existing file is already 24-bit/192kHz+ OST — no sources tried.")
        return results

    def _keep_if_better(sr: "SourceResult", label: str) -> bool:
        """
        Evaluate sr against existing_quality.
        Accept if: file is OST (any format) OR file is FLAC.
        Reject if: non-OST and non-FLAC (MP3, AAC, OGG, etc.).
        If accepted and better than existing: keep and append to results.
        If not better: delete the downloaded file and discard.
        Returns True if kept (and caller should check for early-exit).
        """
        # ── OST-or-FLAC filter ───────────────────────────────────────────
        ext = sr.path.suffix.lower()
        is_ost = _is_ost(sr.path.name) or _is_ost(str(sr.path.parent.name))
        is_flac = (ext == ".flac")

        if not (is_ost or is_flac):
            log.info(f"        {sr.path.suffix.upper()} | non-OST non-FLAC — discarded "
                     f"(only OST or FLAC accepted)")
            try:
                sr.path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

        if sr.quality_score > existing_quality:
            tag = "OST" if is_ost else ("FLAC" if is_flac else "?")
            log.info(f"        Quality score: {sr.quality_score} | "
                     f"{sr.path.suffix.upper()} [{tag}] | BETTER than existing ({existing_quality})")
            results.append(sr)
            return True
        else:
            log.info(f"        Quality score: {sr.quality_score} | "
                     f"{sr.path.suffix.upper()} | NOT better than existing ({existing_quality}) — discarded")
            # Delete the downloaded file since it's not better than existing
            try:
                sr.path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    # --- Source 1: Sockseek (HIGHEST PRIORITY) ---
    if not disable_sockseek:
        log.info("  [1/10] Sockseek ...")
        path = sockseek_download(song, song_dir)
        if path and path.exists():
            sr = SourceResult("Sockseek", path)
            if _keep_if_better(sr, "Sockseek"):
                if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                    log.info("  [TARGET MET] 24-bit/192kHz+ OST from Sockseek.")
                    return results
    else:
        log.info("  [1/10] Sockseek disabled")

    # --- Source 2: Internet Archive ---
    log.info("  [2/10] Internet Archive ...")
    path = archive_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("Internet Archive", path)
        if _keep_if_better(sr, "Internet Archive"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from Internet Archive.")
                return results

    # --- Source 3: Free Music Archive ---
    log.info("  [3/10] Free Music Archive ...")
    path = fma_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("Free Music Archive", path)
        if _keep_if_better(sr, "FMA"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from FMA.")
                return results

    # --- Source 4: Jamendo ---
    log.info("  [4/10] Jamendo ...")
    path = jamendo_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("Jamendo", path)
        if _keep_if_better(sr, "Jamendo"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from Jamendo.")
                return results

    # --- Source 5: YouTube (yt-dlp) ---
    log.info("  [5/10] YouTube (yt-dlp) ...")
    path = ytdlp_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("YouTube (yt-dlp)", path)
        if _keep_if_better(sr, "YouTube"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from YouTube.")
                return results

    # --- Source 6: Bandcamp (yt-dlp) ---
    log.info("  [6/10] Bandcamp (yt-dlp) ...")
    path = bandcamp_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("Bandcamp (yt-dlp)", path)
        if _keep_if_better(sr, "Bandcamp"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from Bandcamp.")
                return results

    # --- Source 7: NetEase Cloud Music (网易云音乐) ---
    log.info("  [7/10] NetEase Cloud Music (网易云音乐) ...")
    path = netease_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("NetEase (网易云音乐)", path)
        if _keep_if_better(sr, "NetEase"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from NetEase.")
                return results

    # --- Source 8: QQ Music (QQ音乐) ---
    log.info("  [8/10] QQ Music (QQ音乐) ...")
    path = qqmusic_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("QQ Music (QQ音乐)", path)
        if _keep_if_better(sr, "QQ Music"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from QQ Music.")
                return results

    # --- Source 9: Kugou Music (酷狗音乐) ---
    log.info("  [9/10] Kugou Music (酷狗音乐) ...")
    path = kugou_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("Kugou (酷狗音乐)", path)
        if _keep_if_better(sr, "Kugou"):
            if sr.quality_score >= QUALITY_HIRESFLAC + OST_BONUS:
                log.info("  [TARGET MET] 24-bit/192kHz+ from Kugou.")
                return results

    # --- Source 10: Migu Music (咪咕音乐) ---
    log.info("  [10/10] Migu Music (咪咕音乐) ...")
    path = migu_download(song, song_dir)
    if path and path.exists():
        sr = SourceResult("Migu (咪咕音乐)", path)
        _keep_if_better(sr, "Migu")

    return results


def _select_best(results: list) -> Optional[SourceResult]:
    """Select highest quality result; on tie use first (Sockseek = priority)."""
    if not results:
        return None
    # Stable sort: keeps insertion order on tie (Sockseek first)
    results.sort(key=lambda r: r.quality_score, reverse=True)
    return results[0]


def _cleanup_worse(results: list, best: SourceResult) -> None:
    """Remove audio files from non-winning sources."""
    for sr in results:
        if sr is not best and sr.path and sr.path.exists():
            try:
                sr.path.unlink()
                log.debug(f"  [CLEANUP] Removed lower-quality: {sr.path.name}")
            except Exception:
                pass


def _cleanup_mp3_in_dir(song_dir: Path) -> int:
    """
    Delete all .mp3 files in song_dir EXCEPT OST soundtrack files.
    Returns the number of MP3 files deleted.
    OST MP3 files are kept (OST files accepted in any format).
    Non-OST MP3 files are inferior to FLAC and are removed.
    """
    deleted = 0
    if not song_dir.exists():
        return deleted
    for f in song_dir.iterdir():
        if f.is_file() and f.suffix.lower() == ".mp3":
            # Keep OST MP3 files
            if _is_ost(f.name) or _is_ost(str(f.parent.name)):
                continue
            try:
                f.unlink()
                log.info(f"  [MP3 CLEANUP] Deleted non-OST MP3: {f.name}")
                deleted += 1
            except Exception as e:
                log.warning(f"  [MP3 CLEANUP] Failed to delete {f.name}: {e}")
    return deleted


def _cleanup_video_in_dir(directory: Path, safe_base: str) -> int:
    r"""
    Delete video-format files (.mp4/.mkv/.avi/.webm etc.) left by yt-dlp.
    yt-dlp's FFmpegExtractAudio postprocessor should convert audio to FLAC
    and delete the intermediate file, but sometimes it fails or leaves
    a video file behind.  Video files are never wanted — only audio.
    Returns the number of video files deleted.
    """
    deleted = 0
    if not directory.exists():
        return deleted
    for f in directory.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            # Only delete files matching our download name pattern
            # (avoid deleting other files that might be in the directory)
            if safe_base and not f.name.startswith(safe_base):
                continue
            try:
                f.unlink()
                log.info(f"  [VIDEO REJECT] Deleted video file: {f.name}")
                deleted += 1
            except Exception as e:
                log.warning(f"  [VIDEO REJECT] Failed to delete {f.name}: {e}")
    return deleted


def download_song(song: dict, out_dir: Path, song_index: int, total: int,
                  disable_sockseek: bool = False) -> dict:
    """
    Download one song with quality-upgrade logic (OST priority, FLAC fallback):

    1. Scan song_dir for any existing audio file → get existing_quality.
    2. Delete non-OST .mp3 files found in song_dir (keep OST MP3s).
    3. Launch lyrics fetch in background thread.
    4. Try all 10 sources; only OST files (any format) OR FLAC files strictly
       better than existing_quality are retained.
       Non-OST non-FLAC files (MP3/AAC/OGG/WAV/APE etc.) are rejected.
       OST versions get +30 score bonus for priority over non-OST FLAC.
    5. If a better file is found: replace existing, update report.
       If no better file is found AND an existing file is present: SKIP (keep existing).
       If no existing file and all sources fail: record failure.
    """
    title  = song["title"]
    artist = song["artist"]
    tag    = f"[{song_index}/{total}]"

    song_dir = out_dir / safe_name(f"{title} - {artist}")
    song_dir.mkdir(parents=True, exist_ok=True)

    # ── Delete non-OST .mp3 files in song_dir (keep OST MP3s) ─────────
    mp3_deleted = _cleanup_mp3_in_dir(song_dir)

    # ── Check existing file quality ───────────────────────────────────────────
    existing_audio = get_existing_audio(song_dir)
    existing_quality = score_audio_file(existing_audio) if existing_audio else 0

    if existing_audio:
        log.info(f"{tag} START  {title} - {artist}  "
                 f"[existing: {existing_audio.name}, score={existing_quality}]")
    else:
        log.info(f"{tag} START  {title} - {artist}  [no existing file]")

    # ── Enhance search for OST priority ──────────────────────────────────────
    # If the title contains OST keywords, also try searching with "OST" appended
    # to help find soundtrack versions on Sockseek and other platforms
    is_title_ost = _is_ost(title)
    if is_title_ost:
        # Add OST variant to song dict for search functions to use
        song.setdefault("_ost_queries", [])
        ost_query = f"{title} OST"
        song["_ost_queries"].append(ost_query)

    result = {
        "raw":              song["raw"],
        "title":            title,
        "artist":           artist,
        "audio_path":       str(existing_audio) if existing_audio else None,
        "audio_source":     None,
        "audio_format":     existing_audio.suffix.lstrip(".").upper() if existing_audio else None,
        "quality_score":    existing_quality,
        "is_flac":          existing_audio.suffix.lower() in (".flac", ".wav") if existing_audio else False,
        "is_ost":           (_is_ost(existing_audio.name) or _is_ost(str(existing_audio.parent.name))) if existing_audio else False,
        "meets_target":     meets_target_quality(existing_audio) if existing_audio else False,
        "lyrics_path":      None,
        "lyrics_attempted": False,
        "note":             "",
    }

    # Existing lyrics
    existing_lrc = get_existing_lyrics(song_dir)
    if existing_lrc:
        result["lyrics_path"] = str(existing_lrc)

    # ── Launch lyrics fetch in a background thread (parallel with audio) ──────
    lrc_future_result: dict = {}
    need_lrc = existing_lrc is None  # only fetch if we don't have one yet

    def _fetch_lrc():
        lrc = fetch_lyrics_multi(
            song["title"], song["artist"], song["primary_artist"]
        )
        lrc_future_result["lrc"] = lrc

    lrc_thread = threading.Thread(target=_fetch_lrc, daemon=True)
    lrc_thread.start()

    # ── Try all 10 audio sources (only keeps FLAC files better than existing) ──
    all_results = _try_all_sources(
        song, song_dir,
        disable_sockseek=disable_sockseek,
        existing_quality=existing_quality,
    )

    best = _select_best(all_results)
    if best:
        # Found something better — remove old file if different path
        if existing_audio and existing_audio.exists() and existing_audio != best.path:
            try:
                existing_audio.unlink()
                log.info(f"{tag} REPLACED old file: {existing_audio.name} "
                         f"(score {existing_quality} -> {best.quality_score})")
            except Exception as e:
                log.warning(f"{tag} Could not remove old file: {e}")
        _cleanup_worse(all_results, best)
        meets = meets_target_quality(best.path)
        is_ost_label = _is_ost(best.path.name) or _is_ost(str(best.path.parent.name))
        result.update(
            audio_path    = str(best.path),
            audio_source  = best.source_name,
            audio_format  = best.path.suffix.lstrip(".").upper(),
            quality_score = best.quality_score,
            is_flac       = best.path.suffix.lower() in (".flac", ".wav"),
            is_ost        = is_ost_label,
            meets_target  = meets,
            note          = (
                f"Source: {best.source_name}, score={best.quality_score}"
                + (" [OST]" if is_ost_label else "")
                + (" [24-bit/192kHz+ TARGET MET]" if meets else "")
                + (f" [upgraded from score={existing_quality}]" if existing_audio else "")
            ),
        )
        log.info(f"{tag} AUDIO  {best.source_name} | score={best.quality_score} | "
                 f"{best.path.suffix.upper()} | "
                 f"{'[OST] ' if is_ost_label else ''}"
                 f"{'24bit/192kHz+ OK' if meets else 'below target'}")
    elif existing_audio and existing_audio.exists():
        # No better source found — keep existing file, just mark as skipped
        log.info(f"{tag} SKIP   No better quality found — keeping: {existing_audio.name} "
                 f"(score={existing_quality})")
        result["note"] = (
            f"Kept existing (score={existing_quality}); "
            "no source had higher quality"
        )
    else:
        # No existing file, all sources failed
        result["note"] = "All sources failed"
        log.error(f"{tag} FAIL   No audio found: {title} - {artist}")

    # ── Wait for lyrics thread and write LRC if needed ────────────────────────
    lrc_thread.join(timeout=30)
    result["lyrics_attempted"] = True
    lrc = lrc_future_result.get("lrc")
    if lrc and need_lrc:
        fname    = safe_name(f"{title} - {artist}") + ".lrc"
        lrc_path = song_dir / fname
        lrc_path.write_text(lrc, encoding="utf-8")
        result["lyrics_path"] = str(lrc_path)
        log.info(f"{tag} LRC    Saved: {fname}")
    elif lrc and not need_lrc:
        log.info(f"{tag} LRC    Already exists, skipped fetch.")
    else:
        if not existing_lrc:
            log.warning(f"{tag} LRC    Not found: {title}")

    log.info(f"{tag} DONE   {title} - {artist}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global PROXY, SOCKSEEK_EXE, SOCKSEEK_USER, SOCKSEEK_PASS
    global SOCKSEEK_MINBR, SOCKSEEK_TIMEOUT, SOCKSEEK_CONF, WORKERS
    global CN_ACCELERATE, ARIA2C_EXE, ARIA2C_CONNECTIONS

    parser = argparse.ArgumentParser(
        description="Hi-Res Music Downloader (10 sources, parallel, 24-bit/192kHz target, FLAC only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Parallel mode: all songs download simultaneously.
Lyrics are fetched in parallel with audio search (6-platform cascade).
Sockseek uses 1 slot at a time (Soulseek account limit).

Audio sources (priority order):
  1. Sockseek (Soulseek P2P)  - FLAC, hi-res preferred
  2. Internet Archive          - Public domain FLAC
  3. Free Music Archive        - Free FLAC
  4. Jamendo                   - Free FLAC
  5. YouTube via yt-dlp        - Best audio -> FLAC
  6. Bandcamp via yt-dlp       - Free releases FLAC
  7. NetEase Cloud Music       - FLAC (网易云音乐)
  8. QQ Music                  - FLAC (QQ音乐)
  9. Kugou Music               - FLAC (酷狗音乐)
  10. Migu Music               - Hi-Res FLAC (咪咕音乐)

Lyrics sources (priority order, by library size):
  1. lrclib.net   2. NetEase   3. QQMusic
  4. Musixmatch   5. Genius    6. megalobiz

Supported playlist platforms (auto-detected):
  QQ Music | NetEase Cloud | Kugou | Kuwo | Qishui | Migu
  Spotify  | YouTube Music | Apple Music

Playlist URL (default): {DEFAULT_PLAYLIST_URL}

China Network Acceleration (--cn-accelerate, 推荐国内用户):
  • aria2c 16-thread parallel download  — 3-8x faster for overseas files
  • GitHub mirror proxy                 — fast auto-download of sockseek/aria2c
  • yt-dlp iOS client + 16 fragments    — uses HK/SG CDN edge nodes
  • All FREE, no account or API key needed

Examples:
  python download_music.py
  python download_music.py --cn-accelerate                              # 国内用户推荐
  python download_music.py --cn-accelerate --workers 16
  python download_music.py --playlist-url https://y.qq.com/n/ryqq/playlist/123456
  python download_music.py --playlist-url https://music.163.com/playlist?id=123456
  python download_music.py --playlist-url https://open.spotify.com/playlist/XXXXX
  python download_music.py --playlist-url https://music.youtube.com/playlist?list=XXXXX
  python download_music.py --playlist-url https://music.apple.com/us/playlist/name/pl.XXXXX
  python download_music.py --cn-accelerate --playlist-url https://y.qq.com/n/ryqq/playlist/123456
  python download_music.py --workers 12
  python download_music.py --proxy http://127.0.0.1:7890
  python download_music.py --no-sockseek --workers 16
        """,
    )
    parser.add_argument("--playlist",  "-p", default="playlist.txt",
                        help="Local playlist text file (used when no URL is provided)")
    parser.add_argument("--playlist-url", "-u", default=None,
                        help="Playlist URL — supports QQ Music, NetEase, Kugou, Kuwo, "
                             "Qishui, Migu, Spotify, YouTube Music, Apple Music. "
                             "Set to 'default' to use the built-in default URL.")
    parser.add_argument("--no-url-prompt",  action="store_true",
                        help="Skip interactive URL prompt; use default URL automatically")
    parser.add_argument("--interactive",    action="store_true",
                        help="Force interactive arrow-key menu even if --no-url-prompt is set")
    parser.add_argument("--output",    "-o", default=None,
                        help="Download output directory (auto-detected if not specified)")
    parser.add_argument("--proxy",           default=None,
                        help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--workers",   "-w", type=int, default=8,
                        help="Parallel worker threads for songs (default: 8)")
    parser.add_argument("--no-resume",       action="store_true",
                        help="Re-download all songs (ignore report)")
    parser.add_argument("--start",     "-s", type=int, default=1,
                        help="Start from Nth song (default: 1)")
    parser.add_argument("--limit",     "-l", type=int, default=0,
                        help="Max songs to process (0=all)")

    # Sockseek
    parser.add_argument("--sockseek-exe",         default="")
    parser.add_argument("--sockseek-user",         default="zpf10284140")
    parser.add_argument("--sockseek-pass",         default="zpf123,")
    parser.add_argument("--sockseek-min-bitrate",  type=int, default=3000)
    parser.add_argument("--sockseek-timeout",      type=int, default=180)
    parser.add_argument("--sockseek-conf",         default="")
    parser.add_argument("--no-sockseek",           action="store_true")

    # China network acceleration options
    parser.add_argument(
        "--cn-accelerate", action="store_true",
        help=(
            "Enable China network acceleration (推荐国内用户使用):\n"
            "  • aria2c 16-thread parallel download — 3-8x faster for overseas servers\n"
            "  • GitHub mirror proxy for sockseek/aria2c auto-download\n"
            "  • yt-dlp: 16 fragment threads + iOS client (HK/SG CDN edges)\n"
            "  • All free, no account needed"
        ),
    )
    parser.add_argument(
        "--aria2c-exe", default="",
        help="Path to aria2c executable (auto-detected if not specified)",
    )
    parser.add_argument(
        "--aria2c-connections", type=int, default=16,
        help="aria2c parallel connections per file (default: 16, max recommended: 32)",
    )

    args = parser.parse_args()

    PROXY            = args.proxy
    SOCKSEEK_EXE     = args.sockseek_exe
    SOCKSEEK_USER    = args.sockseek_user
    SOCKSEEK_PASS    = args.sockseek_pass
    SOCKSEEK_MINBR   = args.sockseek_min_bitrate
    SOCKSEEK_TIMEOUT = args.sockseek_timeout
    SOCKSEEK_CONF    = args.sockseek_conf
    WORKERS          = max(1, args.workers)

    # ── CN acceleration setup ─────────────────────────────────────────────────
    CN_ACCELERATE       = args.cn_accelerate
    ARIA2C_EXE          = args.aria2c_exe
    ARIA2C_CONNECTIONS  = max(1, min(args.aria2c_connections, 64))

    if args.no_sockseek:
        SOCKSEEK_USER = ""

    # ── Auto-detect and configure system proxy for ALL network connections ──
    # Detects system proxy (env vars / Windows registry) and applies it to:
    # requests, urllib, yt-dlp, aria2c, sockseek, and all subprocesses.
    # If no proxy detected, ensures direct (no-proxy) connection.
    PROXY = _setup_proxy(args.proxy)

    # ── CN acceleration: probe/install aria2c ─────────────────────────────────
    if CN_ACCELERATE:
        log.info("[CN-ACCEL] China network acceleration ENABLED")
        log.info(f"[CN-ACCEL]   aria2c connections : {ARIA2C_CONNECTIONS}")
        log.info(f"[CN-ACCEL]   GitHub mirror      : {_GITHUB_CN_MIRRORS[0]}")
        log.info(f"[CN-ACCEL]   yt-dlp fragments   : 16 (iOS CDN client)")

        # Try to find aria2c; if missing, auto-install on Windows
        aria2c_path = _find_aria2c()
        if not aria2c_path:
            if sys.platform == "win32":
                aria2c_path = _install_aria2c_windows()
            if aria2c_path:
                ARIA2C_EXE = aria2c_path
                log.info(f"[CN-ACCEL] aria2c ready: {aria2c_path}")
            else:
                log.warning(
                    "[CN-ACCEL] aria2c not found and auto-install failed.\n"
                    "  Falling back to requests (still uses multi-thread yt-dlp).\n"
                    "  Install manually: winget install aria2.aria2\n"
                    "  Or download: https://github.com/aria2/aria2/releases"
                )
        else:
            log.info(f"[CN-ACCEL] aria2c found: {aria2c_path}")
    else:
        log.info(
            "[TIP] 国内用户可加 --cn-accelerate 参数开启高速加速模式 "
            "(aria2c 16线程 + GitHub镜像 + iOS CDN节点)"
        )

    # ── Sockseek initialization (before banner) ──────────────────────────────
    _download_sockseek_if_needed()

    # ── Determine default download directory ──────────────────────────────────
    #    If D: drive exists → D:\MyMusic; otherwise → C:\MyMusic
    _d_drive_exists = os.path.isdir("D:\\")
    _DEFAULT_DOWNLOAD_DIR = "D:\\MyMusic" if _d_drive_exists else "C:\\MyMusic"

    # ── Prepare output dir: interactive selection if not specified via --output ─
    force_menu = getattr(args, "interactive", False)

    if args.output:
        # User specified via command line — use it directly
        out_dir = Path(args.output)
    elif args.no_url_prompt and not force_menu:
        # Non-interactive mode without --output — use auto-detected default
        out_dir = Path(_DEFAULT_DOWNLOAD_DIR)
        log.info(f"[OUTPUT] Auto-detected download directory: {out_dir.resolve()}")
    else:
        # ── Interactive directory selection (arrow-key) ──────────────────────────
        #    Place BEFORE playlist selection, with visual spacing
        _dir_header = [
            f"  {_ANSI_BOLD}请选择下载目录{_ANSI_RESET}",
        ]
        if not _d_drive_exists:
            _dir_header.append(
                f"  {_ANSI_YELLOW}[INFO] D盘不存在，默认目录已改为 C:\\MyMusic{_ANSI_RESET}"
            )

        _dir_input_hints = [
            f"输入自定义下载目录，例如: {_ANSI_GREEN}E:\\Music{_ANSI_RESET} {_ANSI_CYAN}或{_ANSI_RESET} {_ANSI_GREEN}C:\\Users\\用户名\\Music{_ANSI_RESET}",
            f"不存在的目录将自动创建，遇到 MP3 文件将自动删除{_ANSI_CYAN}",
        ]

        print()
        _dir_choice, _dir_input = _arrow_select_or_input(
            header_lines=_dir_header,
            default_text=f"使用默认下载目录{_ANSI_GREEN}{_DEFAULT_DOWNLOAD_DIR}{_ANSI_RESET}",
            default_hint=f"按Enter键确认{_ANSI_RESET}",
            input_hints=_dir_input_hints,
        )

        if _dir_choice == "default":
            out_dir = Path(_DEFAULT_DOWNLOAD_DIR)
        else:
            _dir_input = _dir_input.strip('"').strip("'")
            out_dir = Path(_dir_input)

        print(f"  [INFO] 已选择: {_ANSI_GREEN}{out_dir}{_ANSI_RESET}")
        sys.stdout.flush()

    # ── Create output directory ────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"[OUTPUT] Download directory: {out_dir.resolve()}")

    # ── Scan and delete non-OST MP3 files in the entire download directory ───
    #    This ensures OST-or-FLAC only from the start (keep OST MP3s)
    _total_mp3_deleted = 0
    if out_dir.exists():
        for song_sub in out_dir.iterdir():
            if song_sub.is_dir():
                _del = _cleanup_mp3_in_dir(song_sub)
                _total_mp3_deleted += _del
        # Also check root-level MP3 files (keep OST MP3s)
        for f in out_dir.iterdir():
            if f.is_file() and f.suffix.lower() == ".mp3":
                if _is_ost(f.name) or _is_ost(str(f.parent.name)):
                    continue
                try:
                    f.unlink()
                    _total_mp3_deleted += 1
                except Exception:
                    pass
    if _total_mp3_deleted > 0:
        log.info(f"[MP3 CLEANUP] Deleted {_total_mp3_deleted} non-OST MP3 files from download directory")

    # ── Resume state ──────────────────────────────────────────────────────────
    use_resume = not args.no_resume
    completed: dict = {}
    if use_resume:
        completed = load_report(REPORT_FILE)
        if completed:
            log.info(f"Resume state: {len(completed)} entries loaded")

    # ── Banner: show ALL config info FIRST ────────────────────────────────────
    log.info("=" * 60)
    log.info("Hi-Res Music Downloader  [PARALLEL MODE]  24-bit/192kHz target")
    log.info(f"Workers: {WORKERS} parallel song threads")
    log.info("Audio  : Sockseek | Archive | FMA | Jamendo | YouTube | Bandcamp | NetEase | QQMusic | Kugou | Migu")
    log.info("Lyrics : lrclib | NetEase | QQMusic | Musixmatch | Genius | megalobiz")
    log.info("Playlist: QQ/NetEase/Kugou/Kuwo/Qishui/Migu/Spotify/YTMusic/AppleMusic")
    log.info("=" * 60)

    exe = _find_sockseek_exe()
    if exe and SOCKSEEK_USER:
        log.info(f"Sockseek: {exe}")
        log.info(f"  user=*** | pref-min-bitrate={SOCKSEEK_MINBR} kbps | serialized (1 slot)")
    elif not exe:
        log.warning("Sockseek: NOT FOUND  ->  Archive/FMA/Jamendo/YouTube/Bandcamp fallback")
        log.warning("  Download: https://github.com/fiso64/sockseek/releases")
    else:
        log.info("Sockseek: disabled (no credentials)")

    if not HAS_REQUESTS:
        log.warning("requests not installed. Run: pip install requests")
    if not HAS_YTDLP:
        log.warning("yt-dlp not installed. Run: pip install yt-dlp")

    log.info(f"  Output dir       : {out_dir.resolve()}")
    log.info(f"  Mode             : quality-upgrade (skip only if no better source found)")
    log.info("=" * 60)

    # ── Visual separator, then prompt user for playlist/song URL ───────────────
    playlist_url: Optional[str] = None
    all_songs: list = []

    if args.playlist_url and not force_menu:
        if args.playlist_url.strip().lower() == "default":
            playlist_url = DEFAULT_PLAYLIST_URL
        else:
            playlist_url = args.playlist_url.strip()
    elif force_menu or not args.no_url_prompt:
        # ── Visual separator between directory selection and playlist selection ─
        _sep_line = f"  {_ANSI_DIM}{'─' * 58}{_ANSI_RESET}"
        print()
        print(_sep_line)
        print()

        # ── Platform names displayed in green ────────────────────────────────
        _platforms_green = (
            f"{_green('Spotify')} | {_green('Youtube music')} | {_green('Apple music')} | "
            f"{_green('QQ音乐')} | {_green('酷狗音乐')} | {_green('酷我音乐')} | "
            f"{_green('网易云音乐')} | {_green('汽水音乐')} | {_green('咪咕音乐')}"
        )

        _url_header = [
            f"  {_ANSI_BOLD}请选择或输入歌单链接 / 单首歌曲链接{_ANSI_RESET}",
            f"  支持平台: {_platforms_green}",
            f"  默认歌单链接: {_ANSI_GREEN}{DEFAULT_PLAYLIST_URL}{_ANSI_RESET}",
        ]

        _url_input_hints = [
            f"输入歌单或歌曲链接{_ANSI_RESET}",
            f"例如: {_ANSI_GREEN}https://y.qq.com/n/ryqq/playlist/...{_ANSI_RESET} {_ANSI_CYAN}或{_ANSI_RESET} {_ANSI_GREEN}https://music.163.com/playlist?id=...{_ANSI_RESET}",
        ]

        _url_choice, _url_input = _arrow_select_or_input(
            header_lines=_url_header,
            default_text="使用默认歌单链接",
            default_hint="按 Enter 键使用默认歌单链接",
            input_hints=_url_input_hints,
        )

        if _url_choice == "default":
            playlist_url = DEFAULT_PLAYLIST_URL
            print(f"  [INFO] 已选择: {_ANSI_GREEN}默认歌单链接{_ANSI_RESET}")
        else:
            playlist_url = _url_input
            print(f"  [INFO] 已选择链接: {_ANSI_GREEN}{_url_input}{_ANSI_RESET}")
        sys.stdout.flush()

    # ── Empty line after user selection, then load playlist ──────────────────
    print()
    sys.stdout.flush()

    if playlist_url:
        log.info(f"[PLAYLIST] Using URL: {playlist_url}")
        url_songs = fetch_url_songs(playlist_url)   # supports both playlist & single track
        if url_songs:
            all_songs = url_songs
            # Save fetched list to playlist.txt for reference / resume
            txt_path = Path(args.playlist)
            try:
                lines = [f"{s['raw']}\n" for s in url_songs]
                txt_path.write_text("".join(lines), encoding="utf-8")
                log.info(f"[PLAYLIST] Saved {len(url_songs)} songs to {txt_path}")
            except Exception as e:
                log.warning(f"[PLAYLIST] Could not save playlist file: {e}")
        else:
            log.warning("[PLAYLIST] URL fetch returned 0 songs — falling back to local file")

    if not all_songs:
        log.info(f"[PLAYLIST] Loading from local file: {args.playlist}")
        all_songs = parse_playlist(args.playlist)

    log.info(f"Playlist: {len(all_songs)} songs total")

    start_idx = max(0, args.start - 1)
    all_songs = all_songs[start_idx:]
    if args.limit > 0:
        all_songs = all_songs[:args.limit]

    # ── All songs enter the worker (quality-upgrade logic handles skipping) ──────
    # Songs are never pre-skipped: each worker checks existing file quality
    # and only downloads/replaces if a better source is found.
    todo_songs = list(all_songs)

    log.info(f"  Total to process : {len(todo_songs)}")
    log.info("=" * 60)

    # Shared mutable report (protected by _report_lock)
    report_map: dict = dict(completed)

    # ── Counters (updated inside lock after each song finishes) ───────────────
    counters = {
        "success": 0,
        "flac":    0,
        "hires":   0,
        "lyrics":  0,
        "skip":    0,   # songs where existing file was best (no upgrade)
        "done":    0,   # worker jobs finished
    }
    total_todo = len(todo_songs)

    def _worker(song: dict, idx: int) -> dict:
        """Worker function called per song in the thread pool."""
        return download_song(
            song, out_dir,
            song_index=idx,
            total=total_todo,
            disable_sockseek=args.no_sockseek,
        )

    # ── Launch all songs in parallel ──────────────────────────────────────────
    future_to_song = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for idx, song in enumerate(todo_songs, 1):
            future = executor.submit(_worker, song, idx)
            future_to_song[future] = song

        # Collect results as they complete
        for future in as_completed(future_to_song):
            song   = future_to_song[future]
            result = None
            try:
                result = future.result()
            except Exception as e:
                log.error(f"[WORKER ERROR] {song['title']} - {song['artist']}: {e}")
                result = {
                    "raw":              song["raw"],
                    "title":            song["title"],
                    "artist":           song["artist"],
                    "audio_path":       None,
                    "audio_source":     None,
                    "audio_format":     None,
                    "quality_score":    0,
                    "is_flac":          False,
                    "is_ost":           False,
                    "meets_target":     False,
                    "lyrics_path":      None,
                    "lyrics_attempted": True,
                    "note":             f"Worker exception: {e}",
                }

            # ── Thread-safe report update ─────────────────────────────────
            with _report_lock:
                report_map[song["raw"]] = result
                counters["done"] += 1
                if result.get("audio_path"):   counters["success"] += 1
                if result.get("is_flac"):      counters["flac"]    += 1
                if result.get("meets_target"): counters["hires"]   += 1
                if result.get("lyrics_path"):  counters["lyrics"]  += 1
                # Count as "skipped" when note indicates no upgrade was needed
                note = result.get("note", "")
                if "Kept existing" in note or "no better source" in note.lower():
                    counters["skip"] += 1

                done_now = counters["done"]
                log.info(
                    f"[PROGRESS] {done_now}/{total_todo} done  "
                    f"| audio={counters['success']}  "
                    f"flac={counters['flac']}  "
                    f"24bit={counters['hires']}  "
                    f"lrc={counters['lyrics']}  "
                    f"kept={counters['skip']}"
                )

                # Persist report after every completion
                save_report(REPORT_FILE, list(report_map.values()))

    # ── Final summary ─────────────────────────────────────────────────────────
    todo_raws = {s["raw"] for s in todo_songs}
    all_results = [r for r in report_map.values() if r.get("raw") in todo_raws]

    failed = [
        r for r in all_results
        if not r.get("audio_path")
        and "Kept existing" not in r.get("note", "")
    ]

    flac_list = [
        r for r in all_results
        if r.get("is_flac") and r.get("audio_path")
    ]

    ost_list = [r for r in all_results if r.get("audio_path") and r.get("is_ost")]

    # ── Build lyrics download list ────────────────────────────────────────────
    lyrics_list = [
        r for r in all_results
        if r.get("lyrics_path")
    ]

    log.info("\n" + "=" * 60)
    log.info("All downloads complete!  Summary:")
    log.info(f"  Total songs        : {total_todo}")
    log.info(f"  Download success   : {counters['success']}")
    log.info(f"  Download failed    : {len(failed)}")
    log.info(f"  Kept (no upgrade)  : {counters['skip']}")
    log.info(f"  FLAC / Lossless    : {counters['flac']}")
    log.info(f"  24-bit/192kHz+     : {counters['hires']}")
    log.info(f"  OST / Soundtrack   : {len(ost_list)}")
    log.info(f"  Lyrics             : {counters['lyrics']}")
    log.info(f"  Report             : {REPORT_FILE.resolve()}")
    log.info(f"  Log                : {LOG_FILE.resolve()}")
    log.info("=" * 60)

    if failed:
        log.info(f"\n[FAILED]  {len(failed)} songs — no audio found on any source:")
        for r in failed:
            log.info(f"  ✗ {r['title']} - {r['artist']}")

    if ost_list:
        log.info(f"\n[OST / Soundtrack]  {len(ost_list)} tracks:")
        for i, r in enumerate(ost_list, 1):
            fmt  = r.get("audio_format") or "?"
            src  = r.get("audio_source") or "?"
            log.info(f"  {i:>3}. {r['title']} - {r['artist']}  [{fmt} | {src}]")

    if flac_list:
        log.info(f"\n[FLAC / Lossless]  {len(flac_list)} tracks:")
        for i, r in enumerate(flac_list, 1):
            score = r.get("quality_score", 0)
            src   = r.get("audio_source") or "?"
            if score >= QUALITY_HIRESFLAC:
                grade = "Hi-Res 24bit/192kHz+"
            elif score >= QUALITY_FLAC96:
                grade = "Hi-Res 24bit/88-96kHz"
            elif score >= QUALITY_FLAC48:
                grade = "24bit/44-48kHz"
            else:
                grade = "16bit FLAC"
            log.info(f"  {i:>3}. {r['title']} - {r['artist']}  [{grade} | {src}]")

    if lyrics_list:
        log.info(f"\n[LYRICS]  {len(lyrics_list)} lyrics downloaded:")
        for i, r in enumerate(lyrics_list, 1):
            lrc_name = Path(r["lyrics_path"]).name if r.get("lyrics_path") else "?"
            log.info(f"  {i:>3}. {r['title']} - {r['artist']}  [{lrc_name}]")


if __name__ == "__main__":
    main()

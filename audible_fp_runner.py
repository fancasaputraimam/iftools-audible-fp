#!/usr/bin/env python3
"""iftools — Audible FP runner.

Thin wrapper around /root/audible_forgot_pw/audible_fp.py so the iftools
console can drive it with the same options as the CLI, streaming progress
to stdout for the web backend to parse.

Usage:
  python audible_fp_runner.py --batch accounts.txt --speed normal
                              [--proxy-file proxies.txt] [--workers N]
                              [--limit N] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# The checker lives in its own project directory with its own venv deps.
AUDIBLE_HOME = Path("/root/audible_forgot_pw")
sys.path.insert(0, str(AUDIBLE_HOME))
os.chdir(AUDIBLE_HOME)


def _mask(line: str) -> str:
    """Mask the password half of an email:pass line for safe logging."""
    m = re.match(r"^([^:]+):(.+)$", line)
    if not m:
        return line
    pw = m.group(2)
    return f"{m.group(1)}:{pw[:2]}{'*' * max(0, min(len(pw) - 2, 8))}"


def _strip_suffix(line: str) -> str:
    line = line.strip()
    return re.sub(r"\s+[—–-]\s*\d+\s*$", "", line)


def load_batch(path: str) -> list:
    accounts = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = _strip_suffix(line)
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                accounts.append(
                    {"email": parts[0].strip(), "imap_pass": parts[1].strip()}
                )
    return accounts


def main() -> int:
    parser = argparse.ArgumentParser(description="iftools audible FP runner")
    parser.add_argument("--batch", required=True, help="accounts.txt path")
    parser.add_argument("--speed", default="normal", help="slow|normal|fast|maximum")
    parser.add_argument("--proxy-file", help="proxies.txt path")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out-dir", default=".", help="Output directory")
    args = parser.parse_args()

    import audible_fp  # noqa: E402  (import after sys.path setup)

    accounts = load_batch(args.batch)
    if not accounts:
        print("[-] no valid accounts in batch file", flush=True)
        return 2

    speed = audible_fp.normalize_speed(args.speed)
    prof = audible_fp.SPEED_PROFILES[speed].copy()
    if args.workers:
        prof["workers"] = args.workers

    proxy_pool = None
    if args.proxy_file and Path(args.proxy_file).exists():
        proxy_pool = audible_fp.load_proxies(args.proxy_file)
        print(
            f"[*] proxy pool: {proxy_pool.size} proxies loaded", flush=True
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[*] audible fp: {len(accounts)} accounts · speed={speed} "
        f"· workers={prof['workers']}",
        flush=True,
    )

    try:
        results, _stats = audible_fp.run_batch(
            accounts,
            prof,
            proxy_pool=proxy_pool,
            limit=args.limit,
            out_dir=str(out_dir),
        )
    except KeyboardInterrupt:
        print("\n[!] interrupted", flush=True)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[-] audible fp failed: {exc}", flush=True)
        return 1

    # Emit a final flat summary the web parser understands:
    #   email:password (status:note)
    # The password is masked here — this line goes verbatim into the console
    # log stream, and the real password already lives in the on-disk
    # results.txt the backend carries over separately.
    for r in results:
        email = r.get("email", "")
        pw = r.get("imap_pass", "") or r.get("password", "")
        label = audible_fp.build_status(r)
        masked = f"{pw[:2]}{'*' * max(0, min(len(pw) - 2, 8))}" if pw else ""
        print(f"{email}:{masked} ({label})", flush=True)

    print(f"[*] done: {len(results)} results", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

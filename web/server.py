#!/usr/bin/env python3
"""FastAPI control plane for the Audible FP Checker (iftools — audible only)."""
from __future__ import annotations

import asyncio
import collections
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
        cmd = [
            sys.executable,
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


@app.get("/api/audible/results")
async def api_audible_results(
    x_access_key: Optional[str] = Header(None),
    kind: str = Query("all", pattern="^(all|hits)$"),
) -> Response:
    _require_auth(x_access_key)
    if not RESULTS_FILE.exists():
        return Response(content="", media_type="text/plain")
    lines = RESULTS_FILE.read_text(encoding="utf-8").splitlines()
    if kind == "hits":
        lines = [ln for ln in lines if "(fail:" not in ln and "(fail " not in ln]
        return Response(content="\n".join(lines), media_type="text/plain")
    # The file is appended on every run, so the same account can appear many
    # times with different labels. Show only the LATEST line per email.
    latest: dict[str, str] = {}
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        em = ln.split(":", 1)[0]
        latest[em] = ln
    return Response(content="\n".join(latest.values()), media_type="text/plain")


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

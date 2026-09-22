"""Manual integration worker — connect existing GitHub accounts.

Used by the web console's Accounts page:

  * codebuddy registration for accounts that do not have it yet
  * 9router injection for accounts already registered at codebuddy

The flow reuses the runner's helpers: launch Camoufox with the current
config (proxy/DataDome trust handling included), log in to GitHub with the
stored credentials, then run the same stage-6/stage-7 code the automatic
pipeline uses.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "ok": 0,
    "fail": 0,
    "current": "",
    "mode": "",
    "error": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "results": [],  # [{email, codebuddy, router9, error}]
}
_thread: Optional[threading.Thread] = None


def get_state() -> dict[str, Any]:
    with _lock:
        return {**_state, "results": list(_state["results"])}


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def _reset(mode: str, total: int) -> None:
    with _lock:
        _state.update(
            running=True,
            total=total,
            done=0,
            ok=0,
            fail=0,
            current="",
            mode=mode,
            error="",
            started_at=time.time(),
            finished_at=0.0,
            results=[],
        )


def _finish(error: str = "") -> None:
    with _lock:
        _state["running"] = False
        _state["finished_at"] = time.time()
        if error:
            _state["error"] = error


def _set_current(email: str) -> None:
    with _lock:
        _state["current"] = email


def _add_result(rec: dict[str, Any]) -> None:
    with _lock:
        _state["results"].append(rec)
        _state["done"] += 1
        if rec.get("error"):
            _state["fail"] += 1
        else:
            _state["ok"] += 1


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _account_records(root: Path) -> list[dict[str, str]]:
    """All GitHub accounts across every accounts file (newest first)."""
    import json

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    acc_dir = root / "accounts"
    files = sorted(acc_dir.glob("github_accounts_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("----")]
            if len(parts) < 3:
                continue
            email = parts[0]
            if email in seen:
                continue
            seen.add(email)
            out.append({
                "email": email,
                "password": parts[1],
                "username": parts[2],
                "totp": parts[3] if len(parts) > 3 else "",
            })
    return out


def _codebuddy_emails(root: Path) -> set[str]:
    """Emails that already have a codebuddy credentials record."""
    import json

    path = root / "accounts" / "codebuddy_accounts.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(d.get("email", "")).strip().lower() for d in data if isinstance(d, dict)}


def _router9_emails(base_url: str, password: str) -> set[str]:
    """Emails already connected as a github provider in 9router."""
    import json
    import urllib.request
    import http.cookiejar

    base = (base_url or "").rstrip("/")
    if not base:
        return set()
    try:
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        body = json.dumps({"password": password}).encode()
        req = urllib.request.Request(
            f"{base}/api/auth/login", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        op.open(req, timeout=15).read()
        req2 = urllib.request.Request(f"{base}/api/providers")
        d = json.loads(op.open(req2, timeout=15).read().decode())
        conns = d.get("connections") or []
        out: set[str] = set()
        for c in conns:
            if not isinstance(c, dict):
                continue
            if str(c.get("provider", "")).lower() != "github":
                continue
            em = str(c.get("email") or "").strip().lower()
            if em:
                out.add(em)
        return out
    except Exception:
        return set()


def _github_totp_code(secret: str) -> Optional[str]:
    import base64
    import hashlib
    import hmac
    import struct

    secret = (secret or "").strip().replace(" ", "")
    if not secret:
        return None
    try:
        key = base64.b32decode(secret)
    except Exception:
        return None
    counter = int(time.time() // 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o : o + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


# --------------------------------------------------------------------------- #
# GitHub login (session only — used for the integration stages)
# --------------------------------------------------------------------------- #


def _login_github(page, context, email: str, password: str, totp: str, log) -> bool:
    """Open github.com/login and submit the stored credentials.

    Handles the optional TOTP (2FA) page. Returns True when logged in.
    """
    from .runner import _logged_in  # type: ignore

    try:
        page.goto("https://github.com/login", wait_until="domcontentloaded", timeout=25000)
    except Exception as exc:
        log(f"[i] {email}: login page unreachable: {exc}")
        return False

    try:
        page.wait_for_selector("#login_field, input[name='login']", state="visible", timeout=10000)
    except Exception:
        # already logged in?
        return _logged_in(context)

    try:
        page.fill("#login_field", email, timeout=5000)
        page.fill("#password", password, timeout=5000)
        time.sleep(0.5)
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='session']");
                if (!form) return;
                let btn = form.querySelector("input[type='submit'], button:not([type='button'])");
                if (!btn) {
                    const btns = form.querySelectorAll("button");
                    btn = btns[btns.length - 1];
                }
                if (btn) btn.click();
            }"""
        )
    except Exception as exc:
        log(f"[i] {email}: login submit failed: {exc}")
        return False

    # wait for either the 2FA page or the logged-in cookie
    deadline = time.time() + 25
    while time.time() < deadline:
        if _logged_in(context):
            return True
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        if "two-factor" in url or "sessions/two" in url:
            break
        time.sleep(1.0)

    # 2FA
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "two-factor" not in url and "sessions/two" not in url:
        return _logged_in(context)

    code = _github_totp_code(totp)
    if not code:
        log(f"[i] {email}: 2FA required but no TOTP secret stored")
        return False
    try:
        page.fill("#app_totp", code, timeout=6000)
        time.sleep(0.5)
        page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('button, input[type=submit]'))
                    .filter(b => /verify|submit|continue/i.test(b.textContent || b.value || ''));
                (btns[0] || document.querySelector('button')).click();
            }"""
        )
    except Exception as exc:
        log(f"[i] {email}: 2FA submit failed: {exc}")
        return False

    deadline = time.time() + 25
    while time.time() < deadline:
        if _logged_in(context):
            return True
        time.sleep(1.0)
    return _logged_in(context)


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #


def run(
    root: Path,
    mode: str,          # "codebuddy" | "router9"
    emails: list[str],  # empty = all eligible
    log: Callable[[str], None],
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> None:
    """Run the integration for the selected accounts.

    mode="codebuddy": accounts WITHOUT a codebuddy record → register them.
    mode="router9"  : accounts WITH a codebuddy record → inject into 9router.
    """
    from camoufox.sync_api import Camoufox

    from .codebuddy import register_codebuddy, save_codebuddy_credentials
    from .router9 import inject_github_provider
    from .runner import _browser_ctx_options  # type: ignore
    from .config import load_config

    cfg = load_config(root / "config.json")
    accounts = _account_records(root)
    cb_emails = _codebuddy_emails(root)
    want = {str(e).strip().lower() for e in emails if str(e).strip()}
    selected = []
    for a in accounts:
        em = a["email"].strip().lower()
        if want and em not in want:
            continue
        if mode == "codebuddy":
            if em in cb_emails:
                continue
        elif mode == "router9":
            if em not in cb_emails:
                continue
        selected.append(a)

    _reset(mode, len(selected))
    if not selected:
        log(f"[i] integration ({mode}): no matching accounts")
        _finish()
        return

    log(f"[*] integration ({mode}): {len(selected)} account(s)")
    r9_base = (cfg.router9_url or "").rstrip("/")
    r9_pass = cfg.router9_password or ""

    # one browser session for the whole batch; every account logs in fresh
    with Camoufox(**_browser_ctx_options(cfg, log=log)) as browser:
        if hasattr(browser, "cookies"):
            context = browser
        else:
            context = browser.new_context(locale="en-US")

        for acc in selected:
            if cancel_cb and cancel_cb():
                log("[!] integration cancelled")
                break
            email = acc["email"]
            _set_current(email)
            log(f"[*] integration ({mode}): {email}")
            rec: dict[str, Any] = {"email": email, "mode": mode,
                                   "codebuddy": False, "router9": False, "error": ""}

            try:
                # fresh session per account
                try:
                    context.clear_cookies()
                except Exception:
                    pass
                page = context.new_page()
                try:
                    ok = _login_github(page, context, email, acc["password"], acc["totp"], log)
                    if not ok:
                        raise RuntimeError("github login failed")
                    log(f"[*] integration: github session active for {email}")

                    if mode == "codebuddy":
                        cb = register_codebuddy(page, context, email, acc["password"], log=log)
                        cb["github_username"] = acc["username"]
                        save_codebuddy_credentials(cb, root / "accounts", log)
                        rec["codebuddy"] = True
                        log(f"[+] integration: codebuddy registered for {email}")

                    if mode == "router9":
                        if not r9_base:
                            raise RuntimeError("router9_url not configured")
                        inject_github_provider(
                            r9_base, r9_pass, context, email, log=log,
                        )
                        rec["router9"] = True
                        log(f"[+] integration: 9router injected for {email}")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            except Exception as exc:
                rec["error"] = str(exc)
                log(f"[-] integration ({mode}): {email} failed: {exc}")

            _add_result(rec)

    _finish()
    log(f"[*] integration ({mode}) done: OK {get_state()['ok']} | FAIL {get_state()['fail']}")


def start(
    root: Path,
    mode: str,
    emails: list[str],
    log: Callable[[str], None],
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> bool:
    """Launch the integration in a background thread. False if already running."""
    global _thread
    with _lock:
        if _state["running"]:
            return False

    def _work() -> None:
        try:
            run(root, mode, emails, log=log, cancel_cb=cancel_cb)
        except Exception as exc:  # noqa: BLE001
            _finish(error=str(exc))

    _thread = threading.Thread(target=_work, daemon=True)
    _thread.start()
    return True

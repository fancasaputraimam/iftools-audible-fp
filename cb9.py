"""Connect an existing CodeBuddy account into 9router as provider=codebuddy-intl.

Flow (9router v0.5.81 codebuddy-intl device-code):
  1. POST  https://www.codebuddy.ai/v2/plugin/auth/state?platform=ide
     → { data: { state, authUrl } }
  2. Browser: open authUrl. If a CodeBuddy session cookie is present the login
     app auto-completes the state handshake (the browser renders the JS app,
     which a plain HTTP GET cannot do).
  3. GET   https://www.codebuddy.ai/v2/plugin/auth/token?state=<state>
     → { code:0, data: { accessToken, refreshToken, ... } }
  4. POST  <9router>/api/oauth/codebuddy-intl/poll
     → 9router stores provider=codebuddy-intl

Usage:
  ./venv python cb9.py --accounts accounts/codebuddy_accounts.json \
      --9router http://127.0.0.1:20128 --r9pass '...' [--only email] [--proxy ip:port]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

CB_STATE = "https://www.codebuddy.ai/v2/plugin/auth/state?platform=ide"
CB_TOKEN = "https://www.codebuddy.ai/v2/plugin/auth/token"
CB_UA = "IDE/2.63.2 CodeBuddy/2.63.2"

_CB_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": CB_UA,
    "X-Requested-With": "XMLHttpRequest",
    "X-Domain": "www.codebuddy.ai",
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
    "X-Product": "SaaS",
}

# GitHub credentials for the Keycloak broker login (filled in main()).
_GH: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# 9router api
# --------------------------------------------------------------------------- #
def _r9(base: str, method: str, path: str, cookies: dict, body: Any = None) -> Any:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            for hdr in r.headers.get_all("Set-Cookie") or []:
                kv = hdr.split(";")[0]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    cookies[k.strip()] = v.strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} HTTP {e.code}: {e.read().decode()[:200]}") from e


def _r9_login(base: str, password: str) -> dict:
    ck: dict[str, str] = {}
    resp = _r9(base, "POST", "/api/auth/login", ck, {"password": password})
    if not (isinstance(resp, dict) and resp.get("success")):
        raise RuntimeError(f"9router login failed: {resp}")
    return ck


# --------------------------------------------------------------------------- #
# codebuddy state / token (plain HTTP)
# --------------------------------------------------------------------------- #
def _req_state() -> str:
    req = urllib.request.Request(
        CB_STATE, data=b"{}",
        headers=_CB_HEADERS, method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.loads(r.read().decode())
    if j.get("code") != 0 or not j.get("data", {}).get("state"):
        raise RuntimeError(f"codebuddy state error: {j}")
    return j["data"]["state"]


def _fetch_token(state: str, deadline: float) -> dict:
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{CB_TOKEN}?state={urllib.parse.quote(state)}",
                    headers=_CB_HEADERS, method="GET",
                ),
                timeout=20,
            ) as r:
                j = json.loads(r.read().decode())
            if j.get("code") == 0 and j.get("data", {}).get("accessToken"):
                return j["data"]
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("codebuddy: token poll timed out (login did not complete)")


# --------------------------------------------------------------------------- #
# GitHub login — the Keycloak broker link needs an active session
# --------------------------------------------------------------------------- #
def _totp_code(secret: str) -> str | None:
    import base64, hashlib, hmac, struct

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


def _logged_in(context) -> bool:
    try:
        for c in context.cookies():
            if c.get("name") == "logged_in" and c.get("value") == "yes":
                return True
    except Exception:
        pass
    return False


def _login_github(page, email: str, password: str, totp: str, log) -> bool:
    """Login to GitHub and wait for the session to FULLY settle.

    The CodeBuddy Keycloak handshake fails when the browser navigates away
    while github.com is still on the two-factor URL — the Keycloak iframe
    disappears and the broker login expires. We wait for a real dashboard
    URL before returning.
    """
    try:
        page.goto("https://github.com/login", wait_until="domcontentloaded", timeout=25000)
    except Exception as exc:  # noqa: BLE001
        log(f"[i] cb9: github login page unreachable: {exc}")
        return False
    if _logged_in(page.context):
        return True
    try:
        page.wait_for_selector("#login_field, input[name='login']", state="visible", timeout=10000)
    except Exception:
        return _logged_in(page.context)
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
    except Exception as exc:  # noqa: BLE001
        log(f"[i] cb9: github submit failed: {exc}")
        return False
    deadline = time.time() + 25
    while time.time() < deadline:
        if _logged_in(page.context):
            break
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        if "two-factor" in url or "sessions/two" in url:
            break
        time.sleep(1.0)
    # TOTP
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "two-factor" in url or "sessions/two" in url:
        code = _totp_code(totp)
        if not code:
            log("[i] cb9: 2FA page but no TOTP secret on file")
            return False
        try:
            # GitHub's 2FA field is name="app_otp" id="app_totp"
            page.wait_for_selector("input[name='app_otp'], #app_totp", state="visible", timeout=8000)
            page.fill("input[name='app_otp'], #app_totp", code, timeout=5000)
            time.sleep(1.0)
            page.evaluate(
                """() => {
                    const form = document.querySelector("form[action*='two-factor'], form");
                    if (!form) return;
                    const b = form.querySelector("input[type='submit'], button[type='submit']")
                             || form.querySelector("button");
                    if (b) b.click();
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[i] cb9: TOTP submit failed: {exc}")
    # CRITICAL: wait until github.com actually leaves the 2FA URL —
    # navigating to codebuddy.ai too early invalidates the Keycloak session.
    t0 = time.time()
    while time.time() - t0 < 20:
        if _logged_in(page.context):
            try:
                cur = page.url or ""
            except Exception:
                cur = ""
            if "two-factor" not in cur and "sessions/two" not in cur:
                return True
        time.sleep(1.0)
    return _logged_in(page.context)


# --------------------------------------------------------------------------- #
# browser step — render the authUrl so the login app finalises the state
# --------------------------------------------------------------------------- #
def _browser_login(state: str, cookies: list[dict], proxy: str | None, log) -> None:
    """Login to GitHub, then complete the CodeBuddy state handshake.

    The Keycloak broker link needs an ACTIVE GitHub session in the same
    browser context, otherwise the broker click just lands on
    github.com/login and the state never completes.
    """
    from camoufox.sync_api import Camoufox

    kw: dict[str, Any] = {"headless": True, "humanize": False}
    if proxy:
        # accept 'http://user:pass@host:port' or 'host:port'
        from urllib.parse import urlparse

        p = proxy.strip()
        if "://" not in p:
            p = "http://" + p
        u = urlparse(p)
        px: dict[str, str] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            px["username"] = u.username
        if u.password:
            px["password"] = u.password
        kw["proxy"] = px
        kw["geoip"] = True  # match proxy locale/timezone (anti-detect)

    log(f"[*] cb9: browser opening auth url (state {state[:8]}…)")
    with Camoufox(**kw) as browser:
        ctx = browser.new_context(locale="en-US")
        # NOTE: do NOT inject the stored codebuddy cookies — they contain a
        # stale Keycloak auth session (KC_RESTART / AUTH_SESSION_ID) that makes
        # Keycloak resume an expired flow and show "Page has expired".
        # The fresh GitHub login + broker click creates a new Keycloak session.
        page = ctx.new_page()
        try:
            # 1) GitHub session first — the broker link redirects to
            #    github.com/login without it.
            if _GH.get("email"):
                log(f"[*] cb9: logging in to GitHub as {_GH['email']}")
                ok_gh = False
                for attempt in range(3):
                    ok_gh = _login_github(page, _GH["email"], _GH["password"],
                                          _GH.get("totp", ""), log)
                    if ok_gh:
                        break
                    log(f"[i] cb9: github login retry {attempt + 1}/3")
                    time.sleep(3)
                log(f"[*] cb9: github session: {'OK' if ok_gh else 'FAILED'} (url {page.url[:60]})")

            # 2) CodeBuddy state handshake
            url = f"https://www.codebuddy.ai/login?platform=ide&state={state}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                log(f"[i] cb9: goto slow: {exc}")

            # the Keycloak iframe renders async — wait for the broker links
            t0 = time.time()
            while time.time() - t0 < 30:
                try:
                    n = page.evaluate(
                        """() => {const f=document.querySelector('iframe');
                            if(!f||!f.contentDocument) return 0;
                            return Array.from(f.contentDocument.querySelectorAll('a'))
                                .filter(a => /broker\\/github\\/login/.test(a.href||'')).length;}"""
                    )
                    if n:
                        break
                except Exception:
                    pass
                time.sleep(1.5)

            cur = url
            clicked = False
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    cur = page.url or ""
                except Exception:
                    cur = ""
                # agreement checkboxes inside the Keycloak iframe
                try:
                    page.evaluate(
                        """() => {
                            const f = document.querySelector('iframe');
                            if (!f || !f.contentDocument) return;
                            const cbs = Array.from(f.contentDocument.querySelectorAll('input[type=checkbox]'))
                                .filter(i => /agree-policy/i.test(i.id || i.name || ''));
                            for (const c of cbs) if (!c.checked) c.click();
                        }"""
                    )
                except Exception:
                    pass
                # 'Sign up with GitHub' inside the iframe
                if not clicked:
                    try:
                        clicked = bool(
                            page.evaluate(
                                """() => {
                                    const f = document.querySelector('iframe');
                                    if (!f || !f.contentDocument) return false;
                                    const links = Array.from(f.contentDocument.querySelectorAll('a'))
                                        .filter(a => /broker\\/github\\/login/.test(a.href || ''));
                                    if (!links.length) return false;
                                    links[links.length - 1].click();
                                    return true;
                                }"""
                            )
                        )
                        if clicked:
                            log("[*] cb9: 'Sign up with GitHub' clicked")
                    except Exception:
                        pass
                else:
                    # post-click: confirm the account link / authorize
                    try:
                        page.evaluate(
                            """() => {
                                const f = document.querySelector('iframe');
                                const docs = f && f.contentDocument ? [f.contentDocument] : [];
                                docs.push(document);
                                for (const d of docs) {
                                    const bs = Array.from(d.querySelectorAll('button, input[type=submit]'));
                                    const b = bs.find(x => /confirm|continue|get started|sign in|log in|link|authorize/i.test(x.textContent || x.value || ''))
                                             || bs[0];
                                    if (b) { b.click(); break; }
                                }
                            }"""
                        )
                    except Exception:
                        pass
                if "started" in cur or "/home" in cur or "dashboard" in cur:
                    break
                time.sleep(2)
            log(f"[*] cb9: browser settled → {cur[:70]}")
        finally:
            try:
                page.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def connect_one(
    email: str,
    cookies: list[dict],
    r9_base: str,
    r9_pass: str,
    log: Callable[[str], None],
    proxy: str | None = None,
    token_timeout: int = 90,
) -> dict[str, Any]:
    """Connect a CodeBuddy account into 9router as provider=codebuddy-intl.

    Uses 9router's own device-code endpoint so the daemon owns the state and
    polls the token endpoint itself.
    """
    ck = _r9_login(r9_base, r9_pass)
    dc = _r9(r9_base, "GET", "/api/oauth/codebuddy-intl/device-code", ck)
    if not isinstance(dc, dict) or not dc.get("device_code"):
        raise RuntimeError(f"9router codebuddy-intl device-code failed: {dc}")
    state = dc["device_code"]
    log(f"[*] cb9: state acquired for {email} ({state[:8]}…)")

    # Browser: complete the login so codebuddy issues a token for this state.
    _browser_login(state, cookies, proxy, log)

    # 9router polls the token endpoint itself until success.
    deadline = time.time() + token_timeout
    while time.time() < deadline:
        try:
            resp = _r9(r9_base, "POST", "/api/oauth/codebuddy-intl/poll", ck,
                       {"deviceCode": state})
        except Exception as exc:  # noqa: BLE001
            log(f"[i] cb9 poll error: {exc}")
            time.sleep(4)
            continue
        if isinstance(resp, dict) and resp.get("success"):
            log(f"[+] cb9: codebuddy-intl connected in 9router for {email}")
            return {"email": email, "status": "connected",
                    "connection": resp.get("connection") or {}}
        if isinstance(resp, dict) and not resp.get("pending"):
            raise RuntimeError(f"9router authorization failed: {resp}")
        time.sleep(4)
    raise RuntimeError("9router polling timed out before the connection was stored")


def _load_gh_creds(root: Path, email: str) -> dict[str, str]:
    """Find email/password/username/totp for a codebuddy email."""
    acc_dir = root / "accounts"
    for f in sorted(acc_dir.glob("github_accounts_*.txt"), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = [p.strip() for p in line.strip().split("----")]
            if len(parts) < 3 or parts[0].lower() != email.lower():
                continue
            return {
                "email": parts[0],
                "password": parts[1],
                "username": parts[2],
                "totp": parts[3] if len(parts) > 3 else "",
            }
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="accounts/codebuddy_accounts.json")
    ap.add_argument("--9router", default="http://127.0.0.1:20128")
    ap.add_argument("--r9pass", required=True)
    ap.add_argument("--only", help="single email")
    ap.add_argument("--proxy", help="http://user:pass@host:port")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    data = json.loads(Path(args.accounts).read_text(encoding="utf-8"))
    if args.only:
        data = [d for d in data if d["email"] == args.only]
    if args.limit:
        data = data[: args.limit]

    proxy = args.proxy
    if not proxy:
        pf = root / "proxies.txt"
        if pf.is_file():
            for line in pf.read_text(errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    proxy = line
                    break

    ok = fail = 0
    for a in data:
        email = a["email"]
        cookies = [c for c in a.get("cookies", []) if "codebuddy.ai" in (c.get("domain") or "")]
        _GH.clear()
        _GH.update(_load_gh_creds(root, email))
        if not _GH.get("email"):
            print(f"[-] cb9: {email} FAILED: no github credentials on file")
            fail += 1
            continue
        try:
            connect_one(email, cookies, getattr(args, "9router"),
                        args.r9pass, log=print, proxy=proxy)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[-] cb9: {email} FAILED: {exc}")
            fail += 1
    print(f"\n[*] cb9: done — OK {ok} | FAIL {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

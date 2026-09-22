"""CodeBuddy.ai auto-registration — GitHub OAuth via Keycloak broker.

Flow (runs while the freshly-registered GitHub session is still active):
  1. GET https://www.codebuddy.ai/login (redirects to Keycloak auth page)
  2. On the Keycloak page: check the 2 mandatory agreement checkboxes,
     then click "Sign up with GitHub" (broker/github/login).
  3. GitHub OAuth authorize page — the browser is already logged in, so the
     authorize button is clicked automatically (or the OAuth grant is
     implicit if the app was already authorized).
  4. Keycloak may ask to link/confirm the account → continue.
  5. Back at codebuddy.ai → session cookie acquired.
  6. Extract and persist credentials (cookies + any API tokens surfaced in
     the dashboard / local storage).

Everything is done inside the same Camoufox context the GitHub registration
used, so no re-login is needed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

CODEBUDDY_BASE = "https://www.codebuddy.ai"
KEYCLOAK_AUTH = f"{CODEBUDDY_BASE}/auth/realms/copilot/protocol/openid-connect/auth"
LOGIN_URL = f"{CODEBUDDY_BASE}/login"

# Selectors on the Keycloak login page (verified against the live page)
_GITHUB_BROKER_LINKS = [
    'a[href*="/broker/github/login"]',  # "Sign up with GitHub" / "Log in with GitHub"
]
_AGREE_CHECKBOXES = [
    'input#agree-policy-account-service',
    'input#agree-policy-account-privacy',
]
_GITHUB_AUTHORIZE_BUTTONS = [
    'button[name="authorize"][value="1"]',
    'input[name="authorize"][value="1"]',
    'button:has-text("Authorize")',
    'button[data-octo-dimensions]',
]
_CONTINUE_BUTTONS = [
    'button:has-text("Continue")',
    'button:has-text("Continue as")',
    'input[name="continue"]',
    'button[name="continue"]',
]
_DONE_URL_FRAGMENTS = (
    "/login/select",
    "/register/user/complete",
    "/home",
    "/dashboard",
    "/login/first-broker-login",
    "first-broker-login",
)


class CodeBuddyError(RuntimeError):
    pass


def _page_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _kc_js(page, js_expr: str):
    """Evaluate JS inside the Keycloak iframe (same-origin → accessible)."""
    return page.evaluate(
        """(expr) => {
            const f = document.querySelector('iframe');
            if (!f || !f.contentDocument) return null;
            try { return (new Function('d', expr))(f.contentDocument); }
            catch (e) { return null; }
        }""",
        js_expr,
    )


def _kc_check_agreements(page, log) -> bool:
    """Check the 2 mandatory agreement checkboxes inside the Keycloak iframe."""
    js_check = """
        const cbs = Array.from(d.querySelectorAll('input[type=checkbox]'))
            .filter(i => /agree-policy/i.test(i.id || i.name || ''));
        let n = 0;
        for (const c of cbs) { if (!c.checked) { c.click(); } if (c.checked) n++; }
        return n;
    """
    try:
        n = _kc_js(page, js_check)
        if n:
            log(f"[*] codebuddy: {n} agreement checkbox(es) checked (iframe)")
            return True
    except Exception:
        pass
    return False


def _kc_click_github(page, log) -> bool:
    """Click 'Sign up with GitHub' inside the Keycloak iframe."""
    js_click = """
        const links = Array.from(d.querySelectorAll('a'))
            .filter(a => /broker\\/github\\/login/.test(a.href || ''));
        if (!links.length) return false;
        const up = links.find(a => /sign up/i.test(a.textContent || ''))
                   || links[links.length - 1];
        up.click();
        return true;
    """
    try:
        if _kc_js(page, js_click):
            log("[*] codebuddy: 'Sign up with GitHub' clicked (iframe)")
            return True
    except Exception:
        pass
    return False


def register_codebuddy(
    page,
    context,
    email: str,
    password: str,
    log: Callable[[str], None],
    timeout: int = 120,
) -> dict[str, Any]:
    """Run the CodeBuddy registration inside the active GitHub session.

    Returns a dict with cookies/tokens on success; raises CodeBuddyError.
    """
    deadline = time.time() + timeout
    log(f"[*] codebuddy: opening {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:  # noqa: BLE001
        raise CodeBuddyError(f"codebuddy: login page unreachable: {exc}") from exc

    # Keycloak page loads in an iframe / redirect. Give it time.
    time.sleep(3)

    # ── 1+2. checkboxes + GitHub broker link (both live inside the iframe) ──
    # Wait for the iframe to render its broker links.
    iframe_ready = False
    t0 = time.time()
    while time.time() - t0 < 25:
        _kc_check_agreements(page, log)
        if _kc_click_github(page, log):
            iframe_ready = True
            break
        time.sleep(1.5)
    if not iframe_ready:
        # fallback: broker link on the top document (some renders skip iframe)
        for sel in _GITHUB_BROKER_LINKS:
            try:
                els = page.query_selector_all(sel)
                if els:
                    els[-1].click()
                    iframe_ready = True
                    log("[*] codebuddy: 'Sign up with GitHub' clicked (top doc)")
                    break
            except Exception:
                continue
    if not iframe_ready:
        raise CodeBuddyError("codebuddy: GitHub broker link not found on Keycloak page")

    # ── 3. GitHub OAuth authorize (session is active → button appears) ─────
    def _js_click_any(page, selectors, log) -> bool:
        for sel in selectors:
            try:
                if page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        el.click(); return true;
                    }""",
                    sel,
                ):
                    log(f"[*] codebuddy: clicked {sel}")
                    return True
            except Exception:
                continue
        return False

    while time.time() < deadline:
        time.sleep(1.5)
        url = _page_url(page)
        if any(f in url for f in _DONE_URL_FRAGMENTS):
            break
        _js_click_any(page, _GITHUB_AUTHORIZE_BUTTONS, log)
        _js_click_any(page, _CONTINUE_BUTTONS, log)
        # also try inside the iframe (keycloak "continue/link account" page)
        _kc_js(page, """
            const bs = Array.from(d.querySelectorAll('button, input[type=submit]'))
                .filter(b => !/maintenance-close|cancel/i.test(b.id || b.name || (b.textContent||'').trim()));
            const b = bs.find(x => /confirm|continue|get started|sign in|log in/i.test(x.textContent || x.value || ''))
                     || bs.find(x => x.id === 'kc-login')
                     || bs[0];
            if (b) b.click(); return !!b;
        """)
        if "oauth/authorize" not in url and "/broker/github" not in url:
            if any(f in url for f in _DONE_URL_FRAGMENTS):
                break

    url = _page_url(page)
    if not any(f in url for f in _DONE_URL_FRAGMENTS):
        # last resort: check the iframe URL too (login-actions are inside it)
        try:
            iframe_url = page.evaluate(
                """() => {
                    const f = document.querySelector('iframe');
                    return f && f.contentWindow ? f.contentWindow.location.href : '';
                }"""
            ) or ""
        except Exception:
            iframe_url = ""
        if not any(f in iframe_url for f in _DONE_URL_FRAGMENTS):
            raise CodeBuddyError(
                f"codebuddy: registration did not complete (url={url})"
            )
        url = iframe_url

    # Keycloak first-broker-login: confirm the account link / profile
    if "first-broker-login" in url:
        log("[*] codebuddy: first-broker-login page — confirming")
        for _ in range(6):
            ok = _kc_js(page, """
                const bs = Array.from(d.querySelectorAll('button, input[type=submit]'))
                    .filter(b => !/maintenance-close|cancel/i.test(b.id || (b.textContent||'').trim()));
                const b = bs.find(x => /confirm|continue|get started|sign in|log in/i.test(x.textContent || x.value || ''))
                         || bs.find(x => x.id === 'kc-login')
                         || bs[0];
                if (b) b.click(); return !!b;
            """)
            if not ok:
                break
            time.sleep(2.5)
            new_url = _kc_js(page, "return d.location ? d.location.href : '';") or ""
            if new_url and "first-broker-login" not in new_url:
                log(f"[*] codebuddy: broker login passed → {new_url[:80]}")
                break

    # ── 4. final landing — settle and collect credentials ─────────────────
    time.sleep(2)
    try:
        if "/login/select" in _page_url(page):
            page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('button,a,[role=button]'))
                        .filter(e => /github/i.test(e.textContent || ''));
                    if (els.length) els[0].click();
                }"""
            )
            log("[*] codebuddy: GitHub identity selected")
            time.sleep(2)
    except Exception:
        pass

    try:
        page.goto(f"{CODEBUDDY_BASE}/home", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(2)

    result: dict[str, Any] = {
        "email": email,
        "status": "ok",
        "url": _page_url(page),
        "cookies": [],
        "tokens": {},
    }
    try:
        cookies = context.cookies()
        result["cookies"] = [
            c
            for c in cookies
            if "codebuddy.ai" in (c.get("domain") or "")
        ]
    except Exception as exc:  # noqa: BLE001
        log(f"[i] codebuddy: cookie export failed: {exc}")

    # surface any token the dashboard stored in localStorage
    try:
        for key in ("token", "access_token", "accessToken", "auth_token", "api_key"):
            val = page.evaluate(
                f"window.localStorage.getItem({key!r}) || window.sessionStorage.getItem({key!r})"
            )
            if val:
                result["tokens"][key] = val
    except Exception:
        pass

    if not result["cookies"] and not result["tokens"]:
        raise CodeBuddyError(
            "codebuddy: flow completed but no session credentials were captured"
        )
    log(f"[+] codebuddy: registered — {len(result['cookies'])} cookies, "
        f"{len(result['tokens'])} tokens")
    return result


def save_codebuddy_credentials(
    result: dict[str, Any],
    out_dir: Path,
    log: Callable[[str], None],
) -> Path:
    """Append the CodeBuddy credentials next to the GitHub accounts file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "codebuddy_accounts.json"
    data: list[dict[str, Any]] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        except Exception:
            data = []
    # drop a previous entry for the same email
    data = [d for d in data if d.get("email") != result.get("email")]
    data.append(result)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[+] codebuddy: credentials saved to {path.name}")
    return path

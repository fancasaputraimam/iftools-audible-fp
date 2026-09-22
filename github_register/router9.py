"""9router integration — inject a freshly-registered GitHub account.

9router (v0.5.81) connects GitHub via the OAuth 2.0 **device code flow**:

  1. POST /api/auth/login            → session cookie (auth_token)
  2. GET  /api/oauth/github/device-code
       → {device_code, user_code, verification_uri, codeVerifier}
  3. Browser: open verification_uri, enter user_code, click "Authorize"
     (the Camoufox context is already logged in to GitHub)
  4. POST /api/oauth/github/poll {device_code, codeVerifier}
       → 9router stores the connection; the account is routable.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

_DEFAULT_PASSWORD = "123456"  # 9router INITIAL_PASSWORD default


class Router9Error(RuntimeError):
    pass


def _api(
    base_url: str,
    method: str,
    path: str,
    cookies: dict[str, str],
    body: Optional[dict[str, Any]] = None,
    timeout: int = 30,
) -> Any:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            new_cookies: dict[str, str] = {}
            for hdr in r.headers.get_all("Set-Cookie") or []:
                kv = hdr.split(";")[0]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    new_cookies[k.strip()] = v.strip()
            if new_cookies:
                cookies.update(new_cookies)
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise Router9Error(f"{method} {path} → HTTP {e.code}: {detail}") from e


def _login(base_url: str, password: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    resp = _api(base_url, "POST", "/api/auth/login", cookies, {"password": password})
    if not (isinstance(resp, dict) and resp.get("success")):
        raise Router9Error(f"9router login failed: {resp}")
    return cookies


def _open_verification_page(
    page,
    verification_uri: str,
    user_code: str,
    log: Callable[[str], None],
    timeout: int = 90,
) -> None:
    """Open github.com/login/device and submit the user code."""
    try:
        page.goto(verification_uri, wait_until="domcontentloaded", timeout=25000)
    except Exception as exc:  # noqa: BLE001
        raise Router9Error(f"9router: cannot open {verification_uri}: {exc}") from exc
    time.sleep(1.5)

    deadline = time.time() + timeout
    while time.time() < deadline:
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        # GitHub redirects to /login/device/select_account when multiple
        # sessions exist; submit that form to reach the code-entry page.
        try:
            url0 = page.url or ""
        except Exception:
            url0 = ""
        if "select_account" in url0:
            log("[*] 9router: account picker — continuing")
            try:
                page.evaluate(
                    """
                    () => {
                        const f = document.querySelector('form');
                        if (!f) return;
                        const b = f.querySelector('input[type=submit], button[type=submit]');
                        if (b) b.click();
                    }
                    """
                )
            except Exception:
                pass
            time.sleep(3)

        # GitHub may also bounce to the login page if the session is stale.
        try:
            url0 = page.url or ""
        except Exception:
            url0 = ""
        if "/login" in url0 and "device" not in url0:
            raise Router9Error(
                "9router: GitHub session expired before device authorization"
            )

        # already authorized for this device code?
        if "success" in url or "/login/device/success" in url:
            log("[*] 9router: device authorization confirmed")
            return
        # paste the user code into the input
        # GitHub uses 8 separate input boxes (#user-code-0 … #user-code-7).
        # IMPORTANT: fill them SIMULTANEOUSLY via JS (value setter + events, then
        # blur each). A normal .fill() focuses the box and GitHub's auto-advance
        # handler clobbers the neighbouring values → empty code → "not found".
        filled = False
        try:
            page.wait_for_selector(
                'input.js-user-code-field, input[id^="user-code-"]',
                state="attached",
                timeout=15000,
            )
        except Exception:
            pass
        for attempt in range(3):
            if filled:
                break
            try:
                filled = bool(
                    page.evaluate(
                        """
                        (code) => {
                            const plain = code.replace(/-/g, '');
                            let boxes = Array.from(
                                document.querySelectorAll('input.js-user-code-field')
                            );
                            if (!boxes.length) {
                                boxes = Array.from(
                                    document.querySelectorAll('input[id^="user-code-"]')
                                );
                            }
                            if (boxes.length < plain.length) return false;
                            const fire = (el, type, key) => {
                                el.dispatchEvent(new KeyboardEvent(type, {
                                    key, bubbles: true, cancelable: true,
                                }));
                            };
                            for (let i = 0; i < plain.length; i++) {
                                const el = boxes[i];
                                const ch = plain[i];
                                const setter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                setter.call(el, ch);
                                fire(el, 'keydown', ch);
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                fire(el, 'keyup', ch);
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                el.blur();
                            }
                            return boxes
                                .slice(0, plain.length)
                                .map(b => b.value)
                                .join('') === plain;
                        }
                        """,
                        user_code,
                    )
                )
                if filled:
                    log("[*] 9router: user code entered (8-box form)")
                    break
            except Exception:
                pass
            time.sleep(1.0)

        if not filled:
            try:
                inp = page.wait_for_selector(
                    'input[name="user_code"], #user_code, input[autocomplete="off"]',
                    state="visible",
                    timeout=4000,
                )
                if inp is not None:
                    inp.fill(user_code)
                    filled = True
            except Exception:
                pass
        if not filled:
            try:
                diag = page.evaluate(
                    """
                    () => ({
                        url: location.href,
                        title: document.title,
                        err: (document.querySelector('[role=alert], .flash-error') || {}).textContent || '',
                        inputs: Array.from(document.querySelectorAll('input')).map(i => i.type + '#' + (i.id||i.name||'')),
                        body: document.body.innerText.slice(0, 200),
                    })
                    """
                )
                log(f"[i] 9router: code-entry diag {diag}")
            except Exception:
                pass
            # If we already landed on the confirmation page the code was
            # accepted — GitHub auto-navigated. There is no input box here by
            # design; raising here was the bug (every inject failed with
            # "cannot find the device-code input form" after a good fill).
            if "/login/device/confirmation" in (page.url or ""):
                log("[*] 9router: already on confirmation page — code accepted")
                filled = True
            else:
                raise Router9Error("9router: cannot find the device-code input form")
        time.sleep(0.4)

        # continue / authorize — the visible button is not always enough;
        # POST the confirmation form directly. GitHub's device page uses
        # form action /login/device/confirmation (NOT /authorize).
        authorized = False
        try:
            authorized = bool(
                page.evaluate(
                    """
                    async () => {
                        const form = document.querySelector(
                            'form[action="/login/device/confirmation"]'
                        );
                        if (!form) return false;
                        const fd = new FormData(form);
                        const params = new URLSearchParams();
                        for (const [k, v] of fd.entries()) params.append(k, v);
                        const res = await fetch('/login/device/confirmation', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded',
                            },
                            body: params.toString(),
                            credentials: 'same-origin',
                            redirect: 'follow',
                        });
                        return res.ok && res.url.includes('/login/device/success');
                    }
                    """,
                )
            )
        except Exception:
            pass

        if not authorized:
            # fallback: submit the confirmation form via its real submit button
            # (the button's visible label is empty; match by type/position)
            for sel in (
                'form[action="/login/device/confirmation"] button[type="submit"]',
                'form[action="/login/device/confirmation"] input[type="submit"]',
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Authorize")',
                'input[name="authorize"][value="1"]',
                'button[name="authorize"]',
            ):
                try:
                    el = page.query_selector(sel)
                    if el is not None and el.is_visible():
                        el.click()
                        authorized = True
                        break
                except Exception:
                    pass
        # We are now on the confirmation page (or already authorized).
        # Wait for GitHub to redirect to the success page — no re-entry into
        # the code-fill loop: the input boxes do not exist here and a re-fill
        # was raising a spurious "cannot find the device-code input form".
        try:
            page.wait_for_url("**/login/device/success*", timeout=20000)
            log("[*] 9router: device authorization confirmed")
            return
        except Exception:
            pass
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if "success" in url or "/login/device/success" in url:
            log("[*] 9router: device authorization confirmed")
            return
        # diagnostics: what is actually on the page right now?
        try:
            diag = page.evaluate(
                """
                () => ({
                    url: location.href,
                    title: document.title,
                    forms: Array.from(document.querySelectorAll('form')).map(f => f.action),
                    buttons: Array.from(document.querySelectorAll('button')).map(b => (b.textContent || '').trim().slice(0, 40)),
                    err: (document.querySelector('[role=alert], .flash-error, .text-red') || {}).textContent || '',
                    boxes: document.querySelectorAll('input.js-user-code-field, input[id^=user-code-]').length,
                })
                """
            )
            log(f"[i] 9router: authorize diag {diag}")
        except Exception as exc:  # noqa: BLE001
            log(f"[i] 9router: authorize diag failed {exc}")
    raise Router9Error("9router: device authorization did not complete in time")


def inject_github_provider(
    base_url: str,
    password: str,
    context,
    email: str,
    log: Callable[[str], None],
    timeout: int = 150,
) -> dict[str, Any]:
    """Connect the active GitHub session into a 9router instance.

    ``context`` is the Camoufox browser context already logged in to GitHub.
    """
    password = password or _DEFAULT_PASSWORD
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        raise Router9Error("9router: base url is required")

    cookies = _login(base_url, password)
    log(f"[*] 9router: logged in to dashboard ({base_url})")

    dc = _api(base_url, "GET", "/api/oauth/github/device-code", cookies)
    if not isinstance(dc, dict) or not dc.get("device_code"):
        raise Router9Error(f"9router: device-code request failed: {dc}")
    device_code = dc["device_code"]
    user_code = dc.get("user_code", "")
    verification_uri = dc.get("verification_uri", "https://github.com/login/device")
    code_verifier = dc.get("codeVerifier", "")
    log(f"[*] 9router: device code {user_code} (expires in {dc.get('expires_in')}s)")

    page = context.new_page()
    try:
        _open_verification_page(page, verification_uri, user_code, log)
    finally:
        try:
            page.close()
        except Exception:
            pass

    # poll 9router until the connection is stored
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = _api(
                base_url,
                "POST",
                "/api/oauth/github/poll",
                cookies,
                {"deviceCode": device_code, "codeVerifier": code_verifier},
                timeout=20,
            )
        except Router9Error as exc:
            log(f"[i] 9router poll error: {exc}")
            time.sleep(4)
            continue
        if isinstance(resp, dict) and resp.get("success"):
            log(f"[+] 9router: GitHub provider connected for {email}")
            return {
                "email": email,
                "dashboard": base_url,
                "user_code": user_code,
                "status": "connected",
                "connection": resp.get("connection") or {},
            }
        if isinstance(resp, dict) and not resp.get("pending"):
            raise Router9Error(f"9router: authorization failed: {resp}")
        time.sleep(4)
    raise Router9Error("9router: polling timed out before the connection was stored")

#!/usr/bin/env python3
"""
Manual OTP flow — browser tetap hidup, baca OTP dari /tmp/otp_input.txt
"""
from playwright.sync_api import sync_playwright
import time, os, sys

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "j-plasshenrich@ifmail.app"
OTP_FILE = "/tmp/otp_input.txt"

# Hapus file lama
if os.path.exists(OTP_FILE):
    os.remove(OTP_FILE)

print(f"[*] Email: {EMAIL}")

with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = br.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        locale="de-DE", viewport={"width":1280,"height":800}
    )
    page = ctx.new_page()

    # Nav
    page.goto("https://www.audible.de/sign-in", wait_until="load", timeout=45000)
    time.sleep(3)
    forgot_url = None
    for lnk in page.query_selector_all("a"):
        try:
            href = lnk.get_attribute("href") or ""
            if "forgotpassword" in href.lower():
                forgot_url = href; break
        except: pass

    page.goto(forgot_url, wait_until="load", timeout=45000)
    time.sleep(3)

    el = page.query_selector('input[name="email"]')
    el.click(click_count=3); el.fill(EMAIL)
    page.query_selector('#continue').click()
    time.sleep(4)

    print(f"[*] OTP dikirim ke {EMAIL}")
    print(f"[*] Tulis OTP ke file: echo 'XXXXXX' > {OTP_FILE}")
    print(f"[*] Waiting...")
    sys.stdout.flush()

    # Tunggu file OTP — max 10 menit
    deadline = time.time() + 600
    otp = None
    while time.time() < deadline:
        if os.path.exists(OTP_FILE):
            with open(OTP_FILE) as f:
                otp = f.read().strip()
            if otp:
                print(f"[*] OTP diterima: {otp}")
                break
        time.sleep(2)

    if not otp:
        print("[!] Timeout tunggu OTP")
        br.close(); sys.exit(1)

    # Isi OTP
    otp_field = (page.query_selector('#cvf-input-code') or
                 page.query_selector('input[name="code"]') or
                 page.query_selector('input[type="text"]'))
    otp_field.click(click_count=3)
    otp_field.fill(otp)
    sub = page.query_selector('input[type="submit"]') or page.query_selector('button[type="submit"]')
    sub.click()
    time.sleep(5)

    print(f"[*] URL setelah OTP: {page.url}")
    print(f"[*] BODY:\n{page.inner_text('body')[:1500]}")
    page.screenshot(path="/root/audible_forgot_pw/secondary.png", full_page=True)

    # Dump inputs
    print("\n[*] VISIBLE INPUTS:")
    for i in page.query_selector_all("input"):
        try:
            if i.is_visible():
                print(f"  type={i.get_attribute('type')} name={i.get_attribute('name')} id={i.get_attribute('id')}")
        except: pass

    print("\n[*] VISIBLE SELECTS:")
    for s in page.query_selector_all("select"):
        try:
            if s.is_visible():
                opts = [o.get_attribute("value") for o in s.query_selector_all("option")]
                print(f"  name={s.get_attribute('name')} opts={opts[:8]}")
        except: pass

    br.close()

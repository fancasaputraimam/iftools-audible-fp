# Audible FP Checker

Batch-validate **Audible.de** accounts through the forgot-password flow: fingerprint
stealth, forgot-password navigation, submit detection and IMAP OTP retrieval, all behind
rotating proxies.

This is the **Audible-only** build of iftools. The GitHub Register tooling (account
creation, codebuddy/9router injection, litensi, mailcx, groups) has been removed.

## Features

- **Forgot-password flow** — navigates `audible.de/sign-in`, finds the forgot link,
  fills the email and reads the Amazon response.
- **Accurate fail labels** — `fail:not_amazon` (not registered, detected from the
  Amazon alert text), `fail:amazon_error`, `fail:no_otp`, `fail:no_forgot_url`.
- **IMAP OTP retrieval** — logs into the account's mailbox (T-Online etc.), watches
  **every** folder (inbox, spam, trash — T-Online uses dot-hierarchy names) and
  extracts the code. Hits are saved as `OTP_SMS:xxx-xxx-xx51`.
- **Proxy rotation** — proxies are mandatory (datacenter IPs get HTTP 503 from
  Audible). Supports a single rotating residential gateway or a multi-proxy pool.
- **Live streaming logs** — every checker step is streamed to the web console over
  SSE (`/api/logs`) and shown inline under the checker.
- **Concurrency** — configurable workers, speed profiles, per-run limit.

## Requirements

- Python 3.11+
- [Camoufox](https://camoufox.com/) (stealth Firefox) + Playwright
- FastAPI + uvicorn (web console)

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Download Camoufox once
python -m camoufox fetch
```

## Running

### Web console

```bash
cp .env.example .env   # set GITHUB_REGISTER_USERNAME / GITHUB_REGISTER_PASSWORD
python -m web.server
```

Open `http://127.0.0.1:8093`, sign in, then:

1. **Accounts file** — a `.txt` with one `email:password` per line.
2. **Proxy list** — one proxy URL per line (`scheme://user:pass@host:port`).
   Required. A datacenter IP is blocked by Audible.
3. Pick **speed** and **workers**, hit **Start checker**.

Results stream into the table and the log panel below it. The **Live Logs** tab shows
the same stream full-width.

### CLI (direct, no web console)

```bash
python audible_fp_runner.py --batch accounts.txt \
  --proxy-file proxies.txt --workers 1 --speed normal --out-dir ./out
```

## Result labels

| label | meaning |
| --- | --- |
| `OTP_SMS:xxx-xxx-xx51` | **HIT** — OTP retrieved from the mailbox |
| `fail:not_amazon` | the email is not registered on Amazon (alert detected) |
| `fail:amazon_error` | Amazon returned a generic error / cookie challenge |
| `fail:no_otp` | account exists but no OTP arrived in time |
| `fail:no_forgot_url` | navigation failed (proxy dead or per-IP block) |

## Notes on proxies

A **rotating residential gateway** (single endpoint, new IP per connection) works, but
parallel workers can land on the same exit IP within a short window and hit a per-IP
block. That surfaces as `fail:no_forgot_url`. Use `workers 1` for maximum accuracy, or
provide a large pool.

## Security

- Auth is username+password from `.env` (`GITHUB_REGISTER_USERNAME` /
  `GITHUB_REGISTER_PASSWORD`), constant-time compared, in-memory sessions, per-IP
  rate limit on `/api/auth`.
- Swagger/Redoc are hidden when auth is enabled.
- `proxies.txt`, `audible_results.txt` and `.env` are git-ignored.

## License

MIT

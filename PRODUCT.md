# iftools — PRODUCT.md

## What it is
**iftools** is a self-hosted internal operations console: a calm, single-entry
dashboard that launches and controls batch automation tools running on the
Hiratake infrastructure. It is not a public product. It is the control room the
operator (BOZ) opens to run account pipelines and check their results.

## Who uses it
One operator, from a desktop browser and occasionally a phone. Technical,
impatient, wants to fire a job and read results — no onboarding, no marketing,
no hand-holding. The UI should disappear into the task.

## What it does today
1. **GitHub Register** — bulk-creates GitHub accounts (mailcow email, OTP,
   profile, recovery codes), then auto-registers codebuddy.ai and injects the
   account into 9router. Start/stop, live metrics, streaming log.
2. **Audible FP Checker** — batch-validates Audible.de accounts via the
   forgot-password flow with fingerprint stealth, IMAP OTP retrieval and proxy
   rotation. Upload accounts.txt + proxies.txt, pick speed, watch results land.
3. **Accounts** — a unified list of both tools' results (GitHub rows with
   TOTP/username/group, Audible rows with hit/bad status), with copy, export,
   group assignment, resend-OTP, and manual CodeBuddy/9router integration.
4. **Config** — mail provider, domains, proxy, codebuddy/9router toggles.

## Platform
Web (FastAPI + React/Vite), served at https://regkit.jamurhiratake.com.
Desktop-first; must survive mobile (top bar, bottom nav, drawer).

## Design authority
**Verdana Health Design System** — supplied verbatim by the operator as the
binding design document. Calm, clinical, trustworthy. Deep navy + soft sage on
soft white. Plus Jakarta Sans / DM Sans / Fira Code. 8px radii, gentle
diffused elevation, generous whitespace. Full token table in DESIGN.md.

## Non-goals
- Not a marketing surface. No landing page, no hero copy, no persuasion.
- Not multi-tenant. One user, one console.
- No decorative motion, no gradient text, no glassmorphism for its own sake.

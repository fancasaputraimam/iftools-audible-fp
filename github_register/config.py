"""Configuration loading for the GitHub register toolkit."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Config:
    # Mail provider: "mailcx" (free, default), "litensi" (paid, reliable),
    # or "imap" (self-hosted mailcow etc. via IMAP + catch-all domain)
    mail_provider: str = "imap"  # "mailcx" | "litensi" | "imap"
    # Mail.cx temp email settings
    mailcx_domain: str = ""  # e.g. "lflood.dev"
    # IMAP (self-hosted mailcow / catch-all domain) — DEFAULT: mail.jamurhiratake.com
    imap_host: str = "mail.jamurhiratake.com"
    imap_port: int = 993
    imap_username: str = "ghreg@ifcodex.me"  # catch-all mailbox login (full address)
    imap_password: str = "GhReg2026!CatchAll"
    imap_domain: str = (
        "ifcodex.me,ifmail.app,ifmail.email,ifqmz.tech,ifqmzz.codes,"
        "iflogic.me,iflogic.my.id,ifnote.app,ifshop.my.id,ifnetworks.me,"
        "jamurhiratake.com,quickpost.web.id,snapmail.biz.id,mailsystem.biz.id,"
        "netnode.web.id,panelbotif.my.id,globalsync.web.id,hiratake.tech,if-mail.tech"
    )  # comma-separated => random rotation per registration (all catch-all -> ghreg)
    imap_ssl: bool = True
    # Litensi Mail settings
    litensi_api_id: str = ""
    litensi_api_key: str = ""
    litensi_site: str = ""   # e.g. "github.com"
    litensi_zone: str = ""   # blank = auto-pick cheapest in-stock zone
    register_count: int = 1
    proxy: str = ""
    proxy_file: str = ""     # proxy pool file in project root (one URL per line); overrides proxy
    headless: bool = False
    delay_sec: float = 5.0
    max_username_tries: int = 6
    otp_timeout_sec: int = 240
    browser_profile_dir: str = ".browser-profile"
    # fresh browser per account (incognito-like, zero cached state); the
    # DataDome trust cookie is carried over via .datadome-trust.json so the
    # signup page keeps loading without hard 403s
    fresh_profile: bool = True
    proxy_hard_block_retries: int = 2
    proxy_rate_limit_retries: int = 2
    # post-signup stages (from user recording)
    create_repo: bool = True          # stage 4: create first repository
    repo_name: str = "hello"          # repo name prefix (username-suffix appended on conflict)
    enable_2fa: bool = True           # stage 5: enable TOTP 2FA and store the secret
    set_profile_status: bool = True
    profile_status: str = "On vacation"  # blank disables custom status text
    complete_profile: bool = True
    profile_name: str = ""            # blank = Random User
    profile_bio: str = ""             # blank = ZenQuotes
    profile_location: str = ""        # blank = Random User country
    # ── post-registration integrations ─────────────────────────────────────
    # auto-register codebuddy.ai via GitHub OAuth while the session is active
    codebuddy: bool = True
    # inject the GitHub account into a 9router instance (dashboard API)
    router9_url: str = "http://127.0.0.1:20128"  # local instance
    router9_password: str = "123456"             # INITIAL_PASSWORD default

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = set(cls.__dataclass_fields__)
        mapped = {k: v for k, v in data.items() if k in known}
        return cls(**mapped)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"config not found: {p} (copy config.example.json to config.json and fill it in)"
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    return Config.from_dict(data)

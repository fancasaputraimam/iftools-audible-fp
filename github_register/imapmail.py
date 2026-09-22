"""Custom IMAP mailbox client for self-hosted mail servers (e.g. mailcow).

Uses a catch-all / wildcard domain: any localpart is delivered to a shared
inbox, so mailboxes are implicit (like mail.cx) — generate a random localpart
and start polling via IMAP.

Requires a catch-all address configured on the mail server (mailcow: add a
catch-all @domain pointing to one mailbox).
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import random
import re
import string
import time
from email.header import decode_header, make_header
from typing import Iterable, Optional

from .mail_errors import MailboxCancelled, MailboxTimeoutError

_LOCALPART_CHARS = string.ascii_lowercase + string.digits + "._-"
_LOCALPART_MIN = 6
_LOCALPART_MAX = 16
_GITHUB_CODE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


class ImapMailError(RuntimeError):
    pass


class ImapMailClient:
    """IMAP mailbox client — implicit mailboxes via catch-all domain.

    ``create_mailbox()`` returns (random_localpart@domain, order_id="").
    ``wait_for_code()`` connects via IMAP, polls the catch-all inbox, and
    extracts the 8-digit GitHub launch code from matching messages.
    """

    def __init__(
        self,
        host: str,
        port: int = 993,
        username: str = "",
        password: str = "",
        domain: str = "",
        ssl: bool = True,
    ):
        self.host = host
        self.port = int(port or 993)
        self.username = username
        self.password = password
        # domain: comma-separated list => random rotation per mailbox
        self.domains = [
            d.lstrip("@").strip()
            for d in (domain or "").split(",")
            if d.lstrip("@").strip()
        ]
        self.use_ssl = ssl
        if not (self.host and self.username and self.password and self.domains):
            raise ImapMailError(
                "imap provider needs host, username, password, and domain"
            )

    # ── mailbox creation (implicit via catch-all) ──────────────────────────

    @staticmethod
    def _random_localpart() -> str:
        n = random.randint(_LOCALPART_MIN, _LOCALPART_MAX)
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choice(_LOCALPART_CHARS) for _ in range(n - 1))
        return first + rest

    @property
    def domain(self) -> str:
        """Active domain (random pick when multiple configured)."""
        return random.choice(self.domains)

    def create_mailbox(self) -> tuple[str, str]:
        """Return (address, order_id). Mailbox is implicit (catch-all).

        Domain is picked at random from the configured list so registrations
        do not pile onto a single domain.

        Note: ``order_id`` is the address itself — the runner passes it as the
        first positional arg to ``wait_for_code()``, and this provider keys
        messages by recipient address (not by an external order id).
        """
        address = f"{self._random_localpart()}@{self.domain}"
        return address, address

    # ── connection ──────────────────────────────────────────────────────────

    def _connect(self) -> imaplib.IMAP4:
        if self.use_ssl:
            conn = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            conn = imaplib.IMAP4(self.host, self.port)
        conn.login(self.username, self.password)
        conn.select("INBOX")
        return conn

    # ── message helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _decode(raw: Optional[str]) -> str:
        if not raw:
            return ""
        try:
            return str(make_header(decode_header(raw)))
        except Exception:
            return raw

    def _get_text(self, msg: email.message.Message) -> str:
        parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            parts.append(payload.decode("utf-8", "replace"))
                        except Exception:
                            parts.append(payload.decode("latin-1", "replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                try:
                    parts.append(payload.decode("utf-8", "replace"))
                except Exception:
                    parts.append(payload.decode("latin-1", "replace"))
        return "\n".join(parts)

    @staticmethod
    def extract_github_code(body: str) -> str:
        """Extract an 8-digit GitHub launch/verification code."""
        if not body:
            return ""
        # prefer codes near explicit "code" labels
        for m in re.finditer(
            r"(?:code|Code|CODE|pin|PIN)[^\d\n]{0,40}(?<!\d)(\d{8})(?!\d)", body
        ):
            return m.group(1)
        m = _GITHUB_CODE_RE.search(body)
        return m.group(1) if m else ""

    # ── fetch messages for a specific address ───────────────────────────────

    def get_messages(self, address: str, since_uid: int = 0) -> list[dict]:
        """Fetch unseen messages addressed to ``address`` (UID > since_uid)."""
        out: list[dict] = []
        conn = None
        try:
            conn = self._connect()
            # search by TO header — catch-all delivers everything to INBOX
            status, data = conn.uid("search", None, f'(TO "{address}")')
            if status != "OK" or not data or not data[0]:
                return out
            uids = [u for u in data[0].split() if int(u) > since_uid]
            for uid in uids:
                status, msgdata = conn.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not msgdata or not msgdata[0]:
                    continue
                raw = msgdata[0][1] if isinstance(msgdata[0], tuple) else b""
                msg = email.message_from_bytes(raw)
                out.append(
                    {
                        "uid": int(uid),
                        "subject": self._decode(msg.get("Subject", "")),
                        "from": self._decode(msg.get("From", "")),
                        "to": self._decode(msg.get("To", "")),
                        "date": msg.get("Date", ""),
                        "body": self._get_text(msg),
                    }
                )
        except Exception as exc:  # noqa: BLE001 — surface as client error
            raise ImapMailError(f"imap fetch failed: {exc}") from exc
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn.logout()
                except Exception:
                    pass
        return out

    # ── wait for the GitHub code ────────────────────────────────────────────

    def wait_for_code(
        self,
        address: str,
        order_id: str = "",
        timeout: float = 240.0,
        log: Optional[callable] = None,
        stop: Optional[callable] = None,
        cancel_cb: Optional[callable] = None,
        email: str = "",  # ignored — present for API compat with MailCxClient
        poll_interval: int = 4,
        exclude_codes: Optional[Iterable[str]] = None,
    ) -> str:
        """Poll the catch-all inbox until the GitHub code for ``address``.

        Mirrors MailCxClient.wait_for_code signature so runner.py can call any
        provider the same way.
        """
        skip = {str(c).strip() for c in (exclude_codes or ()) if str(c).strip()}
        deadline = time.monotonic() + max(5.0, float(timeout))
        # NO baseline here: the runner calls create_mailbox() right before
        # submitting the signup form, but GitHub can deliver the launch-code
        # email within ~1s — faster than we reach this poll. If we snapshot
        # the inbox now, the freshly-delivered OTP gets absorbed into the
        # baseline and every later poll (uid > baseline) sees nothing.
        # Instead: scan ALL messages for the address and skip only codes
        # explicitly listed in exclude_codes (already-consumed codes).
        since_uid = 0

        while True:
            if stop and stop():
                raise MailboxCancelled("imap: cancelled while waiting for code")
            if cancel_cb and cancel_cb():
                raise MailboxCancelled("imap: cancelled while waiting for code")
            if time.monotonic() >= deadline:
                raise MailboxTimeoutError(
                    f"imap: code for {address} not received within {timeout:.0f}s"
                )
            try:
                messages = self.get_messages(address, since_uid=since_uid)
            except Exception as exc:
                if log:
                    log(f"[!] imap poll failed: {exc}")
                time.sleep(5)
                continue
            for msg in messages:
                since_uid = max(since_uid, msg["uid"])
                body = msg["body"] or ""
                code = self.extract_github_code(body)
                if not code:
                    continue
                if code in skip:
                    if log:
                        log(f"[*] imap skipped already-used code {code}")
                    continue
                return code
            time.sleep(4)

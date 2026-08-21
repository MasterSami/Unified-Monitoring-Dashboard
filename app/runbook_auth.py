"""Credential check and signed session cookie for the Runbook tab.

The Runbook runs operational scripts against the monitoring tools, so it sits
behind its own login rather than being open to anyone who can reach the
dashboard. Two credential sources are supported, checked in this order:

1. ``RUNBOOK_USERS`` — a ``;``-separated list of ``user=<sha256-hex>`` entries.
   Only the digest of each password is stored, so the .env file never holds a
   readable password. Generate one with::

       python -m app.runbook_auth hash

2. ``RUNBOOK_USER`` / ``RUNBOOK_PASSWORD`` — a single plaintext pair, for a
   quick single-admin setup. Compared in constant time all the same.

A successful login issues an HMAC-signed cookie holding ``user|expiry``. There
is no server-side session table: the signature is the proof, so this works
unchanged across restarts (given a fixed ``RUNBOOK_SECRET``) and costs nothing
per request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

from app.config import Settings

logger = logging.getLogger("runbook.auth")

#: Name of the signed session cookie.
COOKIE_NAME = "samix_runbook"

#: Process-lifetime fallback key, used when RUNBOOK_SECRET is unset. Sessions
#: then simply end when the app restarts.
_EPHEMERAL_SECRET = secrets.token_hex(32)


def password_digest(password: str) -> str:
    """Return the lowercase sha256 hex digest of a password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _configured_users(settings: Settings) -> dict[str, str]:
    """Return ``{username_lower: sha256_hex}`` from the configured credentials.

    Both sources are merged; a plaintext ``RUNBOOK_PASSWORD`` is hashed here so
    downstream verification has exactly one shape to deal with.
    """
    users: dict[str, str] = {}
    for entry in (settings.runbook_users or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, digest = entry.partition("=")
        name, digest = name.strip().lower(), digest.strip().lower()
        if not sep or not name or not digest:
            logger.warning("ignoring malformed RUNBOOK_USERS entry: %r", entry[:32])
            continue
        users[name] = digest

    if settings.runbook_user and settings.runbook_password:
        users.setdefault(
            settings.runbook_user.strip().lower(),
            password_digest(settings.runbook_password),
        )
    return users


def is_configured(settings: Settings) -> bool:
    """True when at least one Runbook admin account exists."""
    return bool(_configured_users(settings))


#: Digest of an unguessable string, compared against when the username is
#: unknown so a bad user and a bad password cost the same.
_ABSENT_DIGEST = password_digest(secrets.token_hex(32))


def verify_credentials(settings: Settings, user: str, password: str) -> str | None:
    """Return the canonical username on success, else None."""
    users = _configured_users(settings)
    name = (user or "").strip().lower()
    expected = users.get(name, _ABSENT_DIGEST)
    ok = hmac.compare_digest(expected, password_digest(password or ""))
    return name if (ok and name in users) else None


# --- Signed session cookie --------------------------------------------------


def _secret(settings: Settings) -> bytes:
    return (settings.runbook_secret or _EPHEMERAL_SECRET).encode("utf-8")


def _sign(secret: bytes, payload: str) -> str:
    mac = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def issue_token(settings: Settings, user: str) -> str:
    """Return a signed ``user|expiry|sig`` token for the session cookie."""
    minutes = max(1, int(settings.runbook_session_minutes))
    expires = int(time.time()) + minutes * 60
    payload = f"{user}|{expires}"
    return f"{payload}|{_sign(_secret(settings), payload)}"


def read_token(settings: Settings, token: str | None) -> str | None:
    """Return the username carried by a valid, unexpired token, else None."""
    if not token:
        return None
    user, sep1, rest = token.partition("|")
    expires_raw, sep2, signature = rest.partition("|")
    if not (sep1 and sep2 and user and signature):
        return None
    try:
        expires = int(expires_raw)
    except ValueError:
        return None
    if expires < time.time():
        return None
    expected = _sign(_secret(settings), f"{user}|{expires_raw}")
    return user if hmac.compare_digest(expected, signature) else None


def cookie_max_age(settings: Settings) -> int:
    """Cookie lifetime in seconds, matching the token expiry."""
    return max(1, int(settings.runbook_session_minutes)) * 60


if __name__ == "__main__":  # pragma: no cover - operator helper
    import getpass
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        name = input("Username: ").strip().lower()
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("passwords do not match")
        print("\nAdd this to your .env (append with ';' for more admins):\n")
        print(f"RUNBOOK_USERS={name}={password_digest(pw)}")
    else:
        print("usage: python -m app.runbook_auth hash")

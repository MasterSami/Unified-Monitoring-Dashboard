"""Zabbix frontend-controller client — used to send a test mail server-side.

Zabbix exposes no ``mediatype.test`` JSON-RPC method: the "Test" button in the
UI is a frontend controller, not an API call. That distinction matters more than
it looks.

Sending the test over SMTP from wherever the dashboard happens to run only works
if *that* machine can reach the mail relay. Over a VPN from home it usually
cannot — port 25 is not routed — so the test fails even though email alerting is
perfectly healthy. Driving the frontend controller instead makes the **Zabbix
server** send the mail, which is both the thing you actually want to verify and
the one machine guaranteed to have a route to the relay.

Three things make this awkward, and each is handled below:

* The controller's action name changed across Zabbix versions, so it is
  discovered at runtime rather than assumed.
* Every POST needs a ``_csrf_token`` scraped from a page the session has loaded.
* On 7.0+ some candidate actions return the SPA page shell instead of a
  controller response; an HTML document is treated as "not this one".

Credit for the approach and the controller/CSRF details: Eng. Ahmed Hussien's
``zabbix_test_mail.py``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

import httpx

logger = logging.getLogger("collector.zabbix.ui")

#: Candidate "open the test dialog" controllers, newest naming first.
EDIT_ACTIONS = (
    "mediatype.test.edit",
    "popup.mediatypetest.edit",
    "popup.mediatype.test.edit",
    "mediatypetest.edit",
)
#: Candidate "send the test" controllers, in the same order.
SEND_ACTIONS = (
    "mediatype.test.send",
    "popup.mediatypetest.send",
    "popup.mediatype.test.send",
    "mediatypetest.send",
)
#: Pages that reliably carry a CSRF token for a logged-in session.
CSRF_PAGES = (
    "zabbix.php?action=mediatype.list",
    "zabbix.php?action=popup&popup=mediatype.test",
    "zabbix.php?action=userprofile.edit",
)
_MISSING_MARKERS = ("page not found", "incorrect action", "unknown action")

_CSRF_PATTERNS = (
    re.compile(r'name=\\?"_csrf_token\\?"\s+value=\\?"([^"\\]+)'),
    re.compile(r'"_csrf_token"\s*:\s*"([^"]+)"'),
    re.compile(r'_csrf_token[\\"\':=\s]+([A-Za-z0-9]{16,})'),
)

#: Emitted whenever `parameters` is omitted from the send payload. It is a
#: webhook-only form array and any scalar value fails validation, so leaving it
#: out is correct and this notice is noise.
_BENIGN_NOTICE = "JSON array input is expected"


class FrontendError(RuntimeError):
    """The frontend could not be driven (login, CSRF, or controller failure)."""


def _extract_csrf(text: str) -> str | None:
    for pattern in _CSRF_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _flatten(obj: object, out: list[str]) -> None:
    """Collect every string and key in a nested JSON structure."""
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            out.append(str(key))
            _flatten(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _flatten(value, out)


def _csrf_from_response(resp: httpx.Response) -> str | None:
    """Find a CSRF token in a popup reply.

    The popup body arrives as escaped JSON, so the token has to be looked for in
    the decoded structure and in the raw text alike.
    """
    candidates: list[str] = []
    try:
        parts: list[str] = []
        _flatten(resp.json(), parts)
        candidates.append("\n".join(parts))
    except (ValueError, json.JSONDecodeError):
        pass
    candidates.append(resp.text.replace('\\"', '"').replace("\\/", "/"))
    candidates.append(resp.text)
    for text in candidates:
        token = _extract_csrf(text)
        if token:
            return token
    return None


def _looks_missing(text: str) -> bool:
    head = text[:800].lower()
    return any(marker in head for marker in _MISSING_MARKERS)


def _is_html_page(text: str) -> bool:
    """True for the SPA page shell, which is not a controller response."""
    head = text.lstrip()[:9].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


class ZabbixFrontend:
    """A logged-in Zabbix frontend session that can fire the media-type test."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        verify: bool = False,
        timeout: float = 60.0,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.user = user
        self.password = password
        self._client = httpx.Client(
            verify=verify,
            timeout=timeout,
            follow_redirects=False,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.csrf: str | None = None
        self.send_action: str | None = None

    def __enter__(self) -> "ZabbixFrontend":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _url(self, action: str) -> str:
        return f"{self.base}/zabbix.php?action={action}"

    # --- Session ------------------------------------------------------------

    def login(self) -> None:
        """Authenticate against the frontend (not the API) and keep the cookie."""
        resp = self._client.post(
            f"{self.base}/index.php?action=login",
            data={
                "name": self.user,
                "password": self.password,
                "enter": "Sign in",
                "autologin": "1",
            },
        )
        resp.raise_for_status()
        if not any(name.startswith("zbx_session") for name in self._client.cookies.keys()):
            raise FrontendError(
                "frontend login failed (no zbx_session cookie) — check the "
                "username and password in servers.yaml"
            )

    def bootstrap_csrf(self) -> str | None:
        """Fetch any CSRF token; even opening the test dialog needs one."""
        for page in CSRF_PAGES:
            try:
                resp = self._client.get(f"{self.base}/{page}")
            except httpx.HTTPError:
                continue
            token = _extract_csrf(resp.text)
            if token:
                self.csrf = token
                return token
        return None

    # --- Controller discovery ----------------------------------------------

    def discover(self, mediatypeid: str) -> None:
        """Work out which test controller this build actually has."""
        if not self.csrf:
            self.bootstrap_csrf()

        for action in EDIT_ACTIONS:
            data = {"mediatypeid": str(mediatypeid)}
            if self.csrf:
                data["_csrf_token"] = self.csrf
            try:
                resp = self._client.post(
                    f"{self._url(action)}&mediatypeid={mediatypeid}", data=data
                )
            except httpx.HTTPError as exc:
                logger.debug("edit controller %s failed: %s", action, exc)
                continue

            if _is_html_page(resp.text):
                logger.debug("edit controller %s returned the page shell", action)
                continue
            if _looks_missing(resp.text):
                logger.debug("edit controller %s is not present", action)
                continue

            self.send_action = action.removesuffix(".edit") + ".send"
            logger.info(
                "zabbix frontend: using %s (from %s)", self.send_action, action
            )
            token = _csrf_from_response(resp)
            if token:
                self.csrf = token
            return

        logger.info("zabbix frontend: no test dialog matched; will try send actions blind")

    # --- The actual test ----------------------------------------------------

    def _candidate_sends(self) -> Iterable[str]:
        if self.send_action:
            yield self.send_action
        for action in SEND_ACTIONS:
            if action != self.send_action:
                yield action

    def send_test(
        self, mediatypeid: str, sendto: str, subject: str, message: str
    ) -> str:
        """Ask Zabbix to send the test mail. Returns its own success message.

        ``parameters`` is deliberately not submitted: it is a webhook-only form
        array, and any scalar value — ``"[]"`` included — fails validation.
        Omitting it only produces a harmless notice, which is filtered out here.
        """
        payload = {
            "mediatypeid": str(mediatypeid),
            "sendto": sendto,
            "subject": subject,
            "message": message,
        }
        if self.csrf:
            payload["_csrf_token"] = self.csrf

        last_error = "no working test controller found"
        for action in self._candidate_sends():
            try:
                resp = self._client.post(self._url(action), data=payload)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue
            if _looks_missing(resp.text) or _is_html_page(resp.text):
                last_error = f"controller '{action}' is not present"
                continue
            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError):
                last_error = (
                    f"non-JSON reply from '{action}': {resp.text.strip()[:160]}"
                )
                continue

            if "error" in data:
                err = data["error"] or {}
                messages = err.get("messages") or []
                text = f"{err.get('title', '')} {' '.join(messages)}".strip()
                if "unauthorized request" in text.lower():
                    text += " (CSRF token rejected — the session lost its form context)"
                raise FrontendError(f"[{action}] {text}")

            self.send_action = action
            messages = (data.get("success") or {}).get("messages") or []
            messages = [m for m in messages if _BENIGN_NOTICE not in m]
            return "; ".join(messages) or "sent"

        raise FrontendError(last_error)

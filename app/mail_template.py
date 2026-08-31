"""Branded email built by SAMI'X — currently the Zabbix "send test mail" action.

Email is not the web. Outlook on Windows renders HTML through Word, which knows
nothing about flexbox, grid, or most of a modern stylesheet, so everything here
is nested tables with inline styles — the shape that survives every client.

Three rules the layout depends on:

* **Inline styles only.** A ``<style>`` block is stripped by several clients,
  Gmail's web view among them.
* **The logo is optional.** Images are blocked by default in most clients and
  SVG renders in almost none, so the brand mark is HTML text by default and
  reads correctly with images off. Drop a PNG at ``app/static/logo.png`` and it
  is embedded (as a CID attachment, the only form Outlook reliably shows) above
  the wordmark.
* **Always send a plain-text part.** Some clients prefer it, and a
  multipart/alternative message scores better with spam filters than HTML alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger("mail")

BRAND = "SAMI'X"
BRAND_LONG = "SAMI'X · Unified Monitoring Dashboard"

# Matches the dashboard's own palette so the mail and the UI read as one thing.
_INK = "#151a22"
_MUTED = "#5a6675"
_FAINT = "#8b95a4"
_BORDER = "#e0e6ee"
_ACCENT = "#2f4b8f"
_PAPER = "#ffffff"
_GROUND = "#f3f5f8"

# Webfonts do not load in most mail clients; this stack resolves everywhere.
_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif"
)

_LOGO_CID = "samix-logo"
_LOGO_PATH = Path(__file__).resolve().parent / "static" / "logo.png"


def _logo_bytes() -> bytes | None:
    """Return the PNG logo if one is present, else None.

    Only PNG: an SVG would silently fail to render in Outlook and Gmail, which
    is worse than the text wordmark it would have replaced.
    """
    try:
        if _LOGO_PATH.is_file():
            return _LOGO_PATH.read_bytes()
    except OSError as exc:  # pragma: no cover - unreadable file is not fatal
        logger.warning("could not read %s: %s", _LOGO_PATH, exc)
    return None


def _wordmark_html(has_logo: bool) -> str:
    """The brand lockup: the PNG when there is one, styled text otherwise."""
    if has_logo:
        return (
            f'<img src="cid:{_LOGO_CID}" width="34" height="34" alt="{BRAND}" '
            'style="display:block;border:0;outline:none;text-decoration:none;'
            'height:34px;width:auto;" />'
        )
    return (
        f'<span style="font-family:{_FONT};font-size:19px;font-weight:700;'
        f"letter-spacing:-0.2px;color:{_ACCENT};line-height:1;\">{BRAND}</span>"
    )


def _fmt(sent_at: datetime) -> str:
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _html(instance: str, stamp: str, has_logo: bool) -> str:
    """The memo: wordmark, one clear statement, the facts, a signature."""
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{BRAND} test mail</title>
</head>
<body style="margin:0;padding:0;background:{_GROUND};">

<!-- Inbox preview line; hidden in the body itself. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;
  font-size:1px;line-height:1px;color:{_GROUND};">
  Email alerting through {instance} is working.
</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
  style="background:{_GROUND};width:100%;">
<tr>
<td align="center" style="padding:32px 16px;">

  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="520"
    style="width:520px;max-width:100%;background:{_PAPER};
    border:1px solid {_BORDER};border-radius:10px;">

    <tr>
      <td style="padding:38px 40px 0 40px;">
        {_wordmark_html(has_logo)}
      </td>
    </tr>

    <tr>
      <td style="padding:30px 40px 0 40px;font-family:{_FONT};font-size:20px;
        font-weight:600;line-height:1.35;color:{_INK};letter-spacing:-0.2px;">
        Email alerting is working.
      </td>
    </tr>

    <tr>
      <td style="padding:14px 40px 0 40px;font-family:{_FONT};font-size:15px;
        line-height:1.65;color:{_MUTED};">
        This test mail was sent through <span style="color:{_INK};font-weight:600;"
        >{instance}</span> and reached you.<br>
        Mail is working good.
      </td>
    </tr>

    <tr>
      <td style="padding:20px 40px 0 40px;font-family:{_FONT};font-size:13px;
        line-height:1.7;color:{_FAINT};">
        Sent {stamp}
      </td>
    </tr>

    <tr>
      <td style="padding:26px 40px 0 40px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="height:1px;background:{_BORDER};line-height:1px;
            font-size:0;">&nbsp;</td></tr>
        </table>
      </td>
    </tr>

    <tr>
      <td style="padding:18px 40px 34px 40px;font-family:{_FONT};font-size:12px;
        line-height:1.7;color:{_FAINT};">
        <span style="color:{_ACCENT};font-weight:600;">{BRAND}</span>
        &nbsp;·&nbsp; Unified Monitoring Dashboard<br>
        No action is needed.
      </td>
    </tr>

  </table>

</td>
</tr>
</table>

</body>
</html>"""


def _text(instance: str, stamp: str) -> str:
    """Plain-text alternative — the same memo, same order."""
    return f"""\
{BRAND}

Email alerting is working.

This test mail was sent through {instance} and reached you.
Mail is working good.

Sent:  {stamp}

--
{BRAND_LONG}
No action is needed.
"""


def build_test_mail(
    *,
    instance: str,
    sender: str,
    recipient: str,
    sent_at: datetime | None = None,
) -> EmailMessage:
    """Return a ready-to-send branded test mail.

    The result is ``multipart/alternative`` (plain text + HTML); when a PNG logo
    is present the HTML part becomes ``multipart/related`` carrying it, which is
    the arrangement Outlook needs to show an embedded image.
    """
    stamp = _fmt(sent_at or datetime.now(timezone.utc))
    logo = _logo_bytes()

    msg = EmailMessage()
    msg["Subject"] = f"[{BRAND}] Test mail from {instance}"
    msg["From"] = sender
    msg["To"] = recipient
    msg["X-Mailer"] = BRAND_LONG
    # Marks the message as machine-generated so mailing lists and out-of-office
    # responders do not reply to it.
    msg["Auto-Submitted"] = "auto-generated"

    msg.set_content(_text(instance, stamp))
    msg.add_alternative(_html(instance, stamp, logo is not None), subtype="html")

    if logo is not None:
        # payload[1] is the HTML part just added; attaching here (not to the
        # message) keeps the image related to the HTML rather than showing up
        # as a separate download.
        msg.get_payload()[1].add_related(
            logo, maintype="image", subtype="png", cid=f"<{_LOGO_CID}>"
        )
    return msg

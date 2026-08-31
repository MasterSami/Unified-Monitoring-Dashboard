"""The branded test mail, and Topology defaulting to the table view."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

SENT = datetime(2026, 8, 21, 14, 32, tzinfo=timezone.utc)


def _mail(**over):
    from app.mail_template import build_test_mail

    kwargs = dict(
        instance="Zabbix-66",
        relay="10.19.42.5:25",
        sender="zabbix@corp.com",
        recipient="ahmed@corp.com",
        sent_at=SENT,
    )
    kwargs.update(over)
    return build_test_mail(**kwargs)


def _part(msg, subtype):
    return next(
        p for p in msg.walk()
        if p.get_content_maintype() == "text" and p.get_content_subtype() == subtype
    )


# --- Structure --------------------------------------------------------------


def test_mail_is_multipart_alternative_with_both_parts():
    """Clients that prefer plain text must have a real text part to fall back to."""
    msg = _mail()
    assert msg.get_content_type() == "multipart/alternative"
    subtypes = [
        p.get_content_subtype() for p in msg.iter_parts()
    ]
    assert subtypes == ["plain", "html"]


def test_headers_identify_the_tool_and_the_instance():
    msg = _mail()
    assert msg["Subject"] == "[SAMI'X] Test mail from Zabbix-66"
    assert msg["To"] == "ahmed@corp.com"
    assert msg["From"] == "zabbix@corp.com"
    # Marks it machine-generated so auto-responders stay quiet.
    assert msg["Auto-Submitted"] == "auto-generated"


# --- Content ----------------------------------------------------------------


def test_both_parts_carry_the_same_facts():
    msg = _mail()
    text = _part(msg, "plain").get_content()
    html = _part(msg, "html").get_content()
    for body in (text, html):
        assert "Zabbix-66" in body
        assert "10.19.42.5:25" in body
        assert "2026-08-21 14:32 UTC" in body
        assert "SAMI'X" in body
    assert "Email alerting is working" in text
    assert "Email alerting is working" in html


def test_signature_explains_why_the_mail_arrived():
    """A test mail nobody asked for is alarming; say who triggered it."""
    text = _part(_mail(), "plain").get_content()
    assert "Send test mail" in text
    assert "No action is needed" in text


def test_timestamp_is_normalized_to_utc():
    from datetime import timedelta

    naive = _part(_mail(sent_at=datetime(2026, 8, 21, 14, 32)), "plain").get_content()
    assert "2026-08-21 14:32 UTC" in naive  # naive input is treated as UTC

    plus_two = datetime(2026, 8, 21, 16, 32, tzinfo=timezone(timedelta(hours=2)))
    shifted = _part(_mail(sent_at=plus_two), "plain").get_content()
    assert "2026-08-21 14:32 UTC" in shifted  # converted, not relabelled


# --- Client compatibility ---------------------------------------------------


def test_html_uses_only_inline_styles_and_tables():
    """Outlook renders through Word: no <style> block, no flex/grid survives."""
    html = _part(_mail(), "html").get_content()
    assert "<style" not in html.lower()
    assert "display:flex" not in html.replace(" ", "")
    assert "display:grid" not in html.replace(" ", "")
    assert "<table" in html and 'cellpadding="0"' in html
    # Layout tables must be announced as presentational for screen readers.
    assert 'role="presentation"' in html


def test_wordmark_renders_without_any_image_by_default(monkeypatch):
    """Images are blocked by default in most clients — the brand must still read."""
    import app.mail_template as mt

    monkeypatch.setattr(mt, "_logo_bytes", lambda: None)
    msg = _mail()
    html = _part(msg, "html").get_content()
    assert "SAMI'X" in html
    assert "cid:" not in html
    # No image part at all.
    assert not [p for p in msg.walk() if p.get_content_maintype() == "image"]


def test_png_logo_is_embedded_as_a_related_cid_part(monkeypatch):
    """Outlook only shows embedded images via CID, never data: URIs."""
    import app.mail_template as mt

    png = b"\x89PNG\r\n\x1a\n" + b"fake"
    monkeypatch.setattr(mt, "_logo_bytes", lambda: png)
    msg = _mail()

    html_part = _part(msg, "html")
    assert f"cid:{mt._LOGO_CID}" in html_part.get_content()

    images = [p for p in msg.walk() if p.get_content_maintype() == "image"]
    assert len(images) == 1
    assert images[0].get_content_subtype() == "png"
    assert images[0]["Content-ID"] == f"<{mt._LOGO_CID}>"
    assert images[0].get_payload(decode=True) == png

    # The image must sit inside the HTML part, not beside it as an attachment.
    related = [p for p in msg.walk() if p.get_content_type() == "multipart/related"]
    assert related, "logo must be related to the HTML part"


def test_an_svg_logo_is_ignored(monkeypatch, tmp_path):
    """SVG renders in almost no mail client; falling back to text is correct."""
    import app.mail_template as mt

    monkeypatch.setattr(mt, "_LOGO_PATH", tmp_path / "logo.png")  # absent
    (tmp_path / "logo.svg").write_text("<svg/>", encoding="utf-8")
    assert mt._logo_bytes() is None


def test_unreadable_logo_falls_back_instead_of_failing_the_send(monkeypatch, tmp_path):
    """A logo we cannot read is a cosmetic problem, not a reason to lose the mail."""
    import app.mail_template as mt

    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG")
    monkeypatch.setattr(mt, "_LOGO_PATH", logo)

    def denied(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(type(logo), "read_bytes", denied)
    assert mt._logo_bytes() is None
    # And the mail still builds, with the text wordmark.
    assert "SAMI'X" in _part(_mail(), "html").get_content()


# --- The collector actually sends it ----------------------------------------


def test_collector_sends_the_branded_message(monkeypatch):
    """send_test_mail must hand the built message to SMTP, not a bare string."""
    from app.collectors.zabbix import ZabbixCollector
    from app.config import get_settings
    from app.servers import ServerConfig

    collector = ZabbixCollector(
        ServerConfig(name="Zabbix-66", platform="zabbix", url="http://z"),
        get_settings(),
    )
    monkeypatch.setattr(
        collector, "_rpc",
        lambda *a, **k: [{
            "name": "Email", "status": "0", "smtp_server": "10.19.42.5",
            "smtp_port": "25", "smtp_email": "zabbix@corp.com",
            "smtp_security": "0", "smtp_authentication": "0",
        }],
    )

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            sent["quit"] = True

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    result = collector.send_test_mail("ahmed@corp.com")

    assert result["ok"] is True
    msg = sent["msg"]
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Subject"] == "[SAMI'X] Test mail from Zabbix-66"
    # The relay it actually connected to is the relay it reports.
    assert "10.19.42.5:25" in _part(msg, "plain").get_content()
    assert sent.get("quit") is True


def test_smtp_failure_is_reported_not_raised(monkeypatch):
    from app.collectors.zabbix import ZabbixCollector
    from app.config import get_settings
    from app.servers import ServerConfig

    collector = ZabbixCollector(
        ServerConfig(name="Zabbix-66", platform="zabbix", url="http://z"),
        get_settings(),
    )
    monkeypatch.setattr(
        collector, "_rpc",
        lambda *a, **k: [{
            "name": "Email", "status": "0", "smtp_server": "10.19.42.5",
            "smtp_port": "25", "smtp_security": "0", "smtp_authentication": "0",
        }],
    )

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", boom)
    result = collector.send_test_mail("ahmed@corp.com")
    assert result["ok"] is False
    assert "connection refused" in result["message"]


# --- Topology defaults to the table -----------------------------------------


def test_topology_defaults_to_table_not_graph(client):
    """The graph's force layout hangs a laptop on a large map; table is instant."""
    from app.routers.pages import topology_page
    import inspect

    sig = inspect.signature(topology_page)
    assert sig.parameters["mode"].default == "table"


def _active_tab(body: str) -> str:
    """Return the label of the highlighted Table/Graph tab."""
    import re

    for cls, label in re.findall(
        r'class="(seg-item[^"]*)"[^>]*mode=(?:table|graph)[^>]*>[^<]*?(Table|Graph)', body
    ):
        if "active" in cls:
            return label
    return ""


@pytest.mark.parametrize("query", ["", "?mode=nonsense", "?mode=table"])
def test_topology_lands_on_the_table(client, monkeypatch, query):
    """No mode, or a bogus one, must resolve to the table — never the graph."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "enable_topology", True)
    resp = client.get(f"/topology{query}")
    assert resp.status_code == 200
    assert _active_tab(resp.text) == "Table"


def test_topology_still_honors_an_explicit_graph_request(client, monkeypatch):
    """The graph stays one click away for anyone who wants it."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "enable_topology", True)
    assert _active_tab(client.get("/topology?mode=graph").text) == "Graph"


def test_topology_toggle_lists_table_first(client, monkeypatch):
    """Order encodes the default — the primary view reads first."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "enable_topology", True)
    body = client.get("/topology").text
    assert body.index("Table") < body.index("Graph")

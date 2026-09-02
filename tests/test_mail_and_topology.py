"""The branded test mail, and Topology defaulting to the table view."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

SENT = datetime(2026, 8, 21, 14, 32, tzinfo=timezone.utc)


def _mail(**over):
    from app.mail_template import build_test_mail

    kwargs = dict(
        instance="Zabbix-66",
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
        assert "2026-08-21 14:32 UTC" in body
        assert "SAMI'X" in body
        assert "Email alerting is working" in body
        assert "Mail is working good" in body
        assert "No action is needed" in body


def test_signature_names_the_tool_and_closes_the_loop():
    """The reader must know what sent this and that nothing is expected of them."""
    text = _part(_mail(), "plain").get_content()
    assert "SAMI'X \u00b7 Unified Monitoring Dashboard" in text
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
    assert "Zabbix-66" in _part(msg, "plain").get_content()
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


# --- Test mail sent BY the Zabbix server (frontend controller) --------------


def _collector(monkeypatch, user="ahmad", password="pw"):
    from app.collectors.zabbix import ZabbixCollector
    from app.config import get_settings
    from app.servers import ServerConfig

    return ZabbixCollector(
        ServerConfig(name="Zabbix-66", platform="zabbix",
                     url="https://10.19.42.66:8443", user=user, password=password),
        get_settings(),
    )


_MEDIA = [{
    "mediatypeid": "1", "name": "Email", "status": "0",
    "smtp_server": "10.19.42.5", "smtp_port": "25",
    "smtp_email": "zabbix@corp.com", "smtp_security": "0",
    "smtp_authentication": "0",
}]


def test_frontend_is_preferred_so_the_zabbix_server_sends_the_mail(monkeypatch):
    """Over a VPN the relay is unreachable from here but not from Zabbix."""
    import app.collectors.zabbix_ui as ui_mod

    collector = _collector(monkeypatch)
    monkeypatch.setattr(collector, "_rpc", lambda *a, **k: _MEDIA)

    calls = {}

    class FakeUI:
        def __init__(self, base, user, password, **kw):
            calls["base"] = base
            calls["user"] = user

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def login(self):
            calls["login"] = True

        def discover(self, mediatypeid):
            calls["discovered"] = mediatypeid

        def send_test(self, mediatypeid, sendto, subject, message):
            calls["sent"] = (mediatypeid, sendto, subject, message)
            return "Media type test successful."

    monkeypatch.setattr(ui_mod, "ZabbixFrontend", FakeUI)
    # SMTP must never be touched on this path.
    def no_smtp(*a, **k):
        raise AssertionError("must not fall back to SMTP when the frontend works")
    monkeypatch.setattr("smtplib.SMTP", no_smtp)

    result = collector.send_test_mail("ahmed@corp.com")
    assert result["ok"] is True
    assert "Sent by Zabbix-66 itself" in result["message"]
    assert calls["login"] and calls["discovered"] == "1"
    mediatypeid, sendto, subject, message = calls["sent"]
    assert sendto == "ahmed@corp.com"
    assert subject == "[SAMI'X] Test mail from Zabbix-66"
    # The controller takes plain text, not a MIME blob.
    assert "Email alerting is working" in message
    assert "Content-Type" not in message


def test_falls_back_to_smtp_when_the_frontend_fails(monkeypatch):
    import app.collectors.zabbix_ui as ui_mod

    collector = _collector(monkeypatch)
    monkeypatch.setattr(collector, "_rpc", lambda *a, **k: _MEDIA)

    class BrokenUI:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def login(self):
            raise ui_mod.FrontendError("no zbx_session cookie")

    monkeypatch.setattr(ui_mod, "ZabbixFrontend", BrokenUI)

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    result = collector.send_test_mail("ahmed@corp.com")
    assert result["ok"] is True
    assert "msg" in sent, "auto mode must fall back to direct SMTP"


def test_ui_mode_reports_the_failure_instead_of_falling_back(monkeypatch):
    """In explicit ui mode a silent SMTP fallback would hide the real problem."""
    import app.collectors.zabbix_ui as ui_mod

    collector = _collector(monkeypatch)
    monkeypatch.setattr(collector.settings, "test_mail_mode", "ui")
    monkeypatch.setattr(collector, "_rpc", lambda *a, **k: _MEDIA)

    class BrokenUI:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def login(self):
            raise ui_mod.FrontendError("frontend login failed")

    monkeypatch.setattr(ui_mod, "ZabbixFrontend", BrokenUI)
    monkeypatch.setattr(
        "smtplib.SMTP",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fallback in ui mode")),
    )

    result = collector.send_test_mail("ahmed@corp.com")
    assert result["ok"] is False
    assert "frontend login failed" in result["message"]


def test_token_only_instance_skips_the_frontend(monkeypatch):
    """An API token cannot log into the frontend, so SMTP is the only option."""
    collector = _collector(monkeypatch, user="", password="")
    monkeypatch.setattr(collector, "_rpc", lambda *a, **k: _MEDIA)

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    assert collector.send_test_mail("ahmed@corp.com")["ok"] is True
    assert "msg" in sent


# --- Frontend controller parsing -------------------------------------------


def test_csrf_is_found_in_every_shape_zabbix_uses():
    from app.collectors.zabbix_ui import _extract_csrf

    assert _extract_csrf('<input name="_csrf_token" value="abc123def456ghij">') \
        == "abc123def456ghij"
    assert _extract_csrf('{"_csrf_token":"tok9876543210abcd"}') == "tok9876543210abcd"
    assert _extract_csrf("nothing here") is None


def test_escaped_json_popup_still_yields_a_token():
    """The popup body arrives escaped; the token has to survive unescaping."""
    import httpx

    from app.collectors.zabbix_ui import _csrf_from_response

    body = '{"body":"<input name=\\"_csrf_token\\" value=\\"zzz1234567890abc\\">"}'
    resp = httpx.Response(200, text=body)
    assert _csrf_from_response(resp) == "zzz1234567890abc"


def test_page_shell_is_not_mistaken_for_a_controller():
    """On 7.0+ some actions return the SPA shell — that is not a match."""
    from app.collectors.zabbix_ui import _is_html_page, _looks_missing

    assert _is_html_page("<!DOCTYPE html><html>...")
    assert _is_html_page("  <html><body>")
    assert not _is_html_page('{"success":{"messages":["ok"]}}')
    assert _looks_missing("Page not found")
    assert _looks_missing("Incorrect action name")
    assert not _looks_missing('{"success":true}')


def test_send_skips_missing_controllers_until_one_answers():
    import httpx

    from app.collectors.zabbix_ui import ZabbixFrontend

    ui = ZabbixFrontend("https://z", "u", "p")
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        tried.append(action)
        if action == "mediatype.test.send":
            return httpx.Response(200, text="Page not found")
        return httpx.Response(200, json={"success": {"messages": ["Media type test successful."]}})

    ui._client = httpx.Client(transport=httpx.MockTransport(handler))
    result = ui.send_test("1", "a@b.com", "subject", "body")
    assert result == "Media type test successful."
    assert tried[0] == "mediatype.test.send"      # first candidate tried first
    assert len(tried) == 2                        # then the next one, which worked
    ui.close()


def test_send_surfaces_a_controller_error():
    import httpx
    import pytest as _pytest

    from app.collectors.zabbix_ui import FrontendError, ZabbixFrontend

    ui = ZabbixFrontend("https://z", "u", "p")
    ui._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={
            "error": {"title": "Cannot test media type",
                      "messages": ["Connection refused"]}})))
    with _pytest.raises(FrontendError, match="Connection refused"):
        ui.send_test("1", "a@b.com", "s", "b")
    ui.close()


def test_benign_parameters_notice_is_filtered_out():
    """Omitting `parameters` is correct; its notice is noise, not a result."""
    import httpx

    from app.collectors.zabbix_ui import ZabbixFrontend

    ui = ZabbixFrontend("https://z", "u", "p")
    ui._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"success": {"messages": [
            "JSON array input is expected.", "Media type test successful."]}})))
    assert ui.send_test("1", "a@b.com", "s", "b") == "Media type test successful."
    ui.close()


def test_login_without_a_session_cookie_is_an_error():
    import httpx
    import pytest as _pytest

    from app.collectors.zabbix_ui import FrontendError, ZabbixFrontend

    ui = ZabbixFrontend("https://z", "u", "p")
    ui._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="Incorrect user name or password")))
    with _pytest.raises(FrontendError, match="zbx_session"):
        ui.login()
    ui.close()

"""Unit tests for the SiteScope canonical pipeline (redact / parse / severity).

Fixtures use the real (already-redacted) field layout captured from the live
log, plus a synthetic-credential line to prove redaction removes secrets.
"""

from __future__ import annotations

from app.sitescope import (
    STATE_OK,
    dedup_key,
    event_id,
    parse_line,
    redact,
    resolve_severity,
)


def _line(fields: list[str]) -> str:
    """Build a 23-field tab-delimited line from a value list."""
    padded = fields + [""] * (23 - len(fields))
    return "\t".join(padded)


# A real event (NORMAL + "back to default"), field 15 already redacted.
REAL_FIELDS = [
    "2026/08/03 15:21:48:969",
    "NORMAL",
    "SiteScope",
    "SiteScope:sis-01.example.corp:MISOPS: Application: RemdMB-APP6:RemdMB-APP6_script: Queue Depth",
    "10.0.0.201 ,",
    "Monitor (RemdMB-APP6_script: Queue Depth) met defined threshold (back to default) with value (null)",
    "TMetric 'Alert has no defined Metric' crossed 'back to default' with value 'null' , Remedy MW , 10.0.0.201 , MISOPS Team ,",
    " ,",
    "Log Monitor ,",
    "10.0.0.201:60d02dce-11cb-48a7-9fff-20e20dedc6ae:Alert has no defined Metric:NORMAL",
    "10.0.0.201:60d02dce-11cb-48a7-9fff-20e20dedc6ae:Alert has no defined Metric",
    "RemdMB-APP6 ,",
    "::null ,",
    "sis-01.example.corp ,",
    "",
    "https://sis-01.example.corp:8443/SiteScope/servlet/Main?activeid=1972184217&sis_silent_login_type=<REDACTED>&login=<REDACTED>&password=<REDACTED>",
    "SiteScopeMonitor:1972184216:1972184217",
    "1",
]

# Same event but with a CRITICAL severity contradicting a cleared state, and a
# drill-down URL carrying SYNTHETIC credentials that redaction must remove.
_FAKE_SECRETS = [
    "ltypeSecretVal", "svcmon", "P%40ssw0rd123", "adminUser", "topSecretPass", "tok_abc123",
]
CONTRADICTION_FIELDS = list(REAL_FIELDS)
CONTRADICTION_FIELDS[0] = "2026/08/03 15:22:03:500"  # distinct time -> distinct event_id
CONTRADICTION_FIELDS[1] = "CRITICAL"
CONTRADICTION_FIELDS[15] = (
    "https://sis.example.corp:8443/SiteScope/servlet/Main?activeid=1&"
    "sis_silent_login_type=ltypeSecretVal&login=svcmon&password=P%40ssw0rd123&"
    "sisUser=adminUser&sisPass=topSecretPass&token=tok_abc123"
)


class TestRedaction:
    def test_removes_all_known_credentials(self):
        line = _line(CONTRADICTION_FIELDS)
        redacted, count = redact(line)
        # No secret value may survive anywhere in the line.
        for secret in _FAKE_SECRETS:
            assert secret not in redacted, f"leaked: {secret}"
        # Sensitive params must be visibly redacted.
        for key in ("login", "password", "sisUser", "sisPass", "token"):
            assert f"{key}=<REDACTED>" in redacted or f"{key.lower()}=<REDACTED>" in redacted.lower()
        assert count >= 5

    def test_basic_auth_userinfo_is_stripped(self):
        s = "see http://svcuser:hunter2@host:8443/x?a=1"
        redacted, count = redact(s)
        assert "svcuser" not in redacted and "hunter2" not in redacted
        assert count == 1

    def test_non_sensitive_fields_untouched(self):
        redacted, count = redact("host=RemdMB-APP6&view=dashboard&activeid=123")
        assert redacted == "host=RemdMB-APP6&view=dashboard&activeid=123"
        assert count == 0

    def test_idempotent(self):
        once, _ = redact(_line(CONTRADICTION_FIELDS))
        twice, n = redact(once)
        assert twice == once and n == 0


class TestSeverityStateWins:
    def test_cleared_state_forces_ok_even_when_critical(self):
        label, sev, resolved = resolve_severity("CRITICAL", "back to default")
        assert (label, sev, resolved) == ("OK", 1, True)

    def test_all_cleared_states(self):
        for state in STATE_OK:
            label, sev, resolved = resolve_severity("CRITICAL", state.upper())
            assert label == "OK" and resolved is True

    def test_real_severity_when_state_active(self):
        assert resolve_severity("CRITICAL", "error")[:2] == ("Critical", 5)
        assert resolve_severity("MAJOR", "")[:2] == ("Major", 4)
        assert resolve_severity("warning", "threshold")[:2] == ("Warning", 2)

    def test_unknown_severity_defaults_to_warning(self):
        label, sev, _ = resolve_severity("WeirdLevel", "active")
        assert sev == 2 and label == "Weirdlevel"


class TestIdentity:
    def test_event_id_stable_and_16_chars(self):
        a = event_id("2026/08/03 15:21:48:969", "RemdMB-APP6", "mon", "back to default")
        b = event_id("2026/08/03 15:21:48:969", "RemdMB-APP6", "mon", "back to default")
        assert a == b and len(a) == 16

    def test_event_id_changes_with_state(self):
        base = ("2026/08/03 15:21:48:969", "RemdMB-APP6", "mon")
        assert event_id(*base, "back to default") != event_id(*base, "error")

    def test_dedup_key_strips_domain_and_lowercases(self):
        assert dedup_key("sis-01.example.corp") == "sis-01"
        assert dedup_key("RemdMB-APP6") == "remdmb-app6"
        assert dedup_key("") == ""


# An ACTIVE critical event on a different host (state 'error' stays Critical).
ACTIVE_FIELDS = list(REAL_FIELDS)
ACTIVE_FIELDS[0] = "2026/08/04 09:15:00:000"
ACTIVE_FIELDS[1] = "CRITICAL"
ACTIVE_FIELDS[3] = "SiteScope:sis-01.example.corp:PAY: Application: PAY-CORE:PAY-CORE_txn: Response Time"
ACTIVE_FIELDS[4] = "10.0.0.55 ,"
ACTIVE_FIELDS[5] = "Monitor (PAY-CORE_txn: Response Time) met defined threshold (error) with value (8500)"
ACTIVE_FIELDS[10] = "10.0.0.55:g:Response Time ms"
ACTIVE_FIELDS[11] = "PAY-CORE ,"


class TestDeriveHosts:
    def test_status_and_fields(self):
        from app.sitescope import derive_hosts

        evs = [
            parse_line(_line(REAL_FIELDS), "SIS-01"),
            parse_line(_line(ACTIVE_FIELDS), "SIS-01"),
        ]
        hosts = {h.hostname: h for h in derive_hosts(evs)}
        assert set(hosts) == {"RemdMB-APP6", "PAY-CORE"}
        assert hosts["RemdMB-APP6"].status == "up"        # all events resolved
        assert hosts["PAY-CORE"].status == "down"          # active Critical
        assert hosts["PAY-CORE"].ip == "10.0.0.55"
        assert hosts["RemdMB-APP6"].group_name == "MISOPS"


class TestParse:
    def test_parses_real_line(self):
        ev = parse_line(_line(REAL_FIELDS), "SIS-01")
        assert ev.host_hostname == "RemdMB-APP6"
        assert ev.dedup_key == "remdmb-app6"
        assert ev.monitor_name.endswith("Queue Depth")
        assert ev.state.lower() == "back to default"
        assert ev.severity_label == "OK" and ev.resolved is True
        assert ev.metric_missing is True
        assert ev.started_at is not None and ev.started_at.tzinfo is not None
        assert ev.raw_payload["target_ip"] == "10.0.0.201"
        assert ev.raw_payload["sitescope_guid"] == "60d02dce-11cb-48a7-9fff-20e20dedc6ae"

    def test_parse_redacts_as_safety_net(self):
        ev = parse_line(_line(CONTRADICTION_FIELDS), "SIS-01")
        blob = ev.title + str(ev.raw_payload)
        for secret in _FAKE_SECRETS:
            assert secret not in blob
        assert "<REDACTED>" in ev.raw_payload["drilldown_url"]

    def test_contradiction_line_is_ok(self):
        ev = parse_line(_line(CONTRADICTION_FIELDS), "SIS-01")
        assert ev.severity_label == "OK" and ev.resolved is True

    def test_short_line_raises(self):
        import pytest

        with pytest.raises(Exception):
            parse_line("a\tb\tc", "SIS-01")

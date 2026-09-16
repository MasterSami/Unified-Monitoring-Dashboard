"""app.dynatrace_probe: diagnosing a host Dynatrace shows that SAMI'X does not.

Built after a report that IPs visible in Dynatrace (10.22.68.11 through .20)
never appeared in the dashboard. The root cause traced to app/collectors
/dynatrace.py's /api/v2/entities call carrying no explicit `from` — Dynatrace's
own default time window applied, and a HOST that hadn't reported within it was
silently absent from the response. This tests the diagnostic that distinguishes
that case from the others an operator would otherwise have to guess between.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from app.db import SessionLocal
from app.dynatrace_probe import _expand_ip_range, probe
from app.models import Host, HostStatus, SourcePlatform

INST = "DT-PROBE-TEST"


class _FakeDynatrace:
    """Answers /api/v2/entities for a small, scripted set of IPs.

    ``visible_narrow`` / ``visible_wide`` are the IPs each call variant would
    find — modelling exactly the distinction the real bug turned on.
    """

    name = "dynatrace"
    _base = "https://fake.example/e/x"

    def __init__(
        self,
        visible_narrow: set[str],
        visible_wide: set[str],
        *,
        all_ips: list[str] | None = None,
    ):
        self.visible_narrow = visible_narrow
        self.visible_wide = visible_wide
        #: The entity's full address list, fixed regardless of which one was
        #: queried — a real host's IPs do not depend on which one you asked
        #: about. Defaults to "the queried address is the only, primary one".
        self.all_ips = all_ips
        self.calls: list[tuple[str, bool]] = []   # (ip, had_from_param)

    def _headers(self) -> dict:
        return {}

    def _client(self, **kwargs):
        import httpx

        return httpx.Client(**kwargs)

    def _request_with_retries(self, client, method, url, **kwargs):
        params = kwargs.get("params", {})
        ip = params["entitySelector"].split('"')[3]
        had_from = "from" in params
        self.calls.append((ip, had_from))
        visible = self.visible_wide if had_from else self.visible_narrow
        ip_list = self.all_ips or [ip]

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                if ip not in visible:
                    return {"entities": []}
                return {
                    "entities": [
                        {
                            "entityId": "HOST-1",
                            "displayName": "rpa-quiet",
                            "properties": {
                                "monitoringMode": "FULL_STACK",
                                "state": "RUNNING",
                                "ipAddress": ip_list,
                            },
                            "managementZones": [{"name": "RPA-ZONE"}],
                        }
                    ]
                }

        return _Resp()


def _run(monkeypatch, fake: _FakeDynatrace, ips: list[str]) -> str:
    import app.scheduler as scheduler

    class _Service:
        collectors = {"DT-1": fake}

    monkeypatch.setattr(scheduler, "_service", _Service())

    db = SessionLocal()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            probe(db, ips)
    finally:
        db.close()
    return buf.getvalue()


class TestIpRangeExpansion:
    def test_expands_inclusive_range(self):
        ips = _expand_ip_range("10.22.68.11-10.22.68.20")
        assert ips == [f"10.22.68.{i}" for i in range(11, 21)]

    def test_single_ip_with_no_dash_passes_through(self):
        assert _expand_ip_range("10.22.68.11") == ["10.22.68.11"]

    def test_reversed_range_is_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="before start"):
            _expand_ip_range("10.22.68.20-10.22.68.11")

    def test_a_huge_range_is_rejected_rather_than_hammering_the_api(self):
        import pytest

        with pytest.raises(ValueError, match="512"):
            _expand_ip_range("10.0.0.1-10.0.255.255")


class TestDiagnosis:
    """The three outcomes an operator actually needs told apart."""

    def test_found_only_wide_names_the_lookback_setting(self, client, monkeypatch):
        """The exact bug this was built for: absent by default, present when widened."""
        fake = _FakeDynatrace(visible_narrow=set(), visible_wide={"10.22.68.11"})
        out = _run(monkeypatch, fake, ["10.22.68.11"])
        assert "DYNATRACE_ENTITY_LOOKBACK_DAYS" in out
        assert "rpa-quiet" in out
        # Both windows were actually queried, not assumed.
        assert ("10.22.68.11", False) in fake.calls
        assert ("10.22.68.11", True) in fake.calls

    def test_found_either_way_says_it_should_already_work(self, client, monkeypatch):
        fake = _FakeDynatrace(
            visible_narrow={"10.22.68.12"}, visible_wide={"10.22.68.12"}
        )
        out = _run(monkeypatch, fake, ["10.22.68.12"])
        assert "should already be collecting" in out
        assert "DYNATRACE_ENTITY_LOOKBACK_DAYS" not in out

    def test_found_nowhere_points_at_scope_and_management_zones(self, client, monkeypatch):
        fake = _FakeDynatrace(visible_narrow=set(), visible_wide=set())
        out = _run(monkeypatch, fake, ["10.22.68.13"])
        assert "not a time-window issue" in out
        assert "management zone" in out
        assert "Verdict for 10.22.68.13" in out

    def test_a_secondary_address_is_labelled_as_such(self, client, monkeypatch):
        """The entity's own IP list says which position this address holds."""
        fake = _FakeDynatrace(
            visible_narrow={"192.168.9.9"}, visible_wide={"192.168.9.9"},
            all_ips=["10.22.68.11", "192.168.9.9"],   # queried address is 2nd
        )
        out = _run(monkeypatch, fake, ["192.168.9.9"])
        assert "secondary" in out

    def test_a_host_already_stored_locally_is_reported_before_querying_dynatrace(
        self, client, monkeypatch
    ):
        db = SessionLocal()
        try:
            db.add(Host(
                hostname="already-known", source_platform=SourcePlatform.dynatrace,
                source_instance=INST, external_id="dt-known-1", status=HostStatus.up,
                ip="10.9.9.9", ip_all="10.9.9.9, 10.9.9.8",
            ))
            db.commit()
        finally:
            db.close()

        fake = _FakeDynatrace(visible_narrow=set(), visible_wide=set())
        # Found via ip_all (a secondary address), not the primary ip column.
        out = _run(monkeypatch, fake, ["10.9.9.8"])
        assert "already in SAMI'X: already-known" in out
        assert "secondary (ip_all)" in out

    def test_multiple_instances_are_each_reported_on(self, client, monkeypatch):
        import app.scheduler as scheduler

        fake_a = _FakeDynatrace(visible_narrow=set(), visible_wide={"10.1.1.1"})
        fake_b = _FakeDynatrace(visible_narrow=set(), visible_wide=set())

        class _Service:
            collectors = {"DT-A": fake_a, "DT-B": fake_b}

        monkeypatch.setattr(scheduler, "_service", _Service())
        db = SessionLocal()
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                probe(db, ["10.1.1.1"])
        finally:
            db.close()
        out = buf.getvalue()
        assert "--- DT-A ---" in out and "--- DT-B ---" in out
        assert "DYNATRACE_ENTITY_LOOKBACK_DAYS" in out   # from DT-A
        assert "not a time-window issue" in out           # from DT-B

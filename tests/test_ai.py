"""SAMIX AI: allow-list, hostname resolution, evidence pack, validator, and
the ask flow end to end with a deterministic fake model (no Ollama)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ai.evidence import EvidencePack, build_pack, freshness_line, validate_answer
from app.ai.fake import FakeLLM
from app.ai.gateway import Gateway, GatewayError, RateLimiter, set_gateway
from app.ai.tools import (
    ToolArgumentError, ToolBroker, ToolResult, UnknownToolError, resolve_hostname, ollama_tool_definitions,
)
from app.config import get_settings
from app.db import SessionLocal
from app.models import AIAudit, AIFeedback, Alert, CollectorRun, Host, HostStatus, RunStatus, SourcePlatform

NOW = datetime.now(timezone.utc)


def _host(db, name, platform=SourcePlatform.zabbix, instance="ZBX-AI", ip=None, status=HostStatus.up):
    h = Host(hostname=name, ip=ip or f"10.77.{len(name) % 250}.{abs(hash(name)) % 250 + 1}",
             source_platform=platform, source_instance=instance,
             external_id=f"{instance}-{name}-{uuid4().hex[:6]}", status=status, group_name="AI Test",
             last_seen=NOW)
    db.add(h)
    db.flush()
    return h


def _alert(db, host: Host, title, sev=4, resolved=False):
    a = Alert(external_id=f"a-{uuid4().hex[:8]}", source_platform=host.source_platform,
              source_instance=host.source_instance, host_hostname=host.hostname, host_ip=host.ip,
              host_external_id=host.external_id, severity_int=sev, severity_label="High",
              title=title, started_at=NOW - timedelta(minutes=30), resolved=resolved)
    db.add(a)
    db.flush()
    return a


@pytest.fixture
def ai_on(monkeypatch):
    """AI enabled, no login needed, fake model; restored afterwards."""
    s = get_settings()
    monkeypatch.setattr(s, "enable_ai", True)
    monkeypatch.setattr(s, "ai_require_login", False)
    monkeypatch.setattr(s, "ai_rate_limit_per_min", 100)
    gw = Gateway(s, llm=FakeLLM())
    set_gateway(gw)
    yield gw
    set_gateway(None)


# --- tools: allow-list and schema --------------------------------------------------


def test_unknown_tool_is_refused_and_audited(client, ai_on):
    broker = ToolBroker(get_settings())
    with pytest.raises(UnknownToolError):
        broker.execute("delete_all_hosts", {})
    with pytest.raises(ToolArgumentError):
        broker.execute("get_host_alerts", {})  # hostname is required

    gw = Gateway(get_settings(), llm=FakeLLM(force_tool=("drop_table", {"name": "hosts"})))
    result = gw.ask("do something bad", user="tester")
    assert result.tools and result.tools[0]["ok"] is False
    assert result.tools[0]["error"].startswith("refused")
    assert any("drop_table could not run" in u for u in result.pack.unknowns)
    db = SessionLocal()
    try:
        row = db.scalar(select(AIAudit).where(AIAudit.trace_id == result.trace_id))
        assert row is not None and row.tools_json[0]["name"] == "drop_table" and row.tools_json[0]["ok"] is False
    finally:
        db.close()


def test_tool_definitions_are_exactly_the_four_read_only_tools():
    names = [t["function"]["name"] for t in ollama_tool_definitions()]
    assert names == ["get_host_alerts", "get_host_status", "get_active_alerts_summary", "get_capacity_risk"]
    for t in ollama_tool_definitions():
        assert t["function"]["parameters"]["type"] == "object"


# --- hostname resolution ---------------------------------------------------------


def test_hostname_resolution_exact_fuzzy_none_and_too_many(client):
    db = SessionLocal()
    try:
        tag = uuid4().hex[:5]
        _host(db, f"mw{tag}-10.corp.local")
        _host(db, f"MW{tag}-10", platform=SourcePlatform.nnmi, instance="NNMI-AI")
        _host(db, f"mw{tag}-10-standby")
        for i in range(7):
            _host(db, f"amb{tag}-{i:02d}")
        db.commit()

        exact = resolve_hostname(db, f"mw{tag}-10")
        assert exact.status == "exact" and len(exact.hosts) == 2          # both platforms, domain stripped
        assert {h.source_platform.value for h in exact.hosts} == {"zabbix", "nnmi"}

        fuzzy = resolve_hostname(db, f"mw{tag}-10-stand")
        assert fuzzy.status == "fuzzy" and [h.hostname for h in fuzzy.hosts] == [f"mw{tag}-10-standby"]

        assert resolve_hostname(db, "no-such-host-anywhere").status == "none"
        assert resolve_hostname(db, "").status == "none"

        many = resolve_hostname(db, f"amb{tag}")
        assert many.status == "ambiguous" and not many.hosts
        assert len(many.candidates) == 6 and many.candidates[-1] == "..."
    finally:
        db.close()


def test_host_alerts_tool_joins_all_platforms_for_the_same_device(client):
    db = SessionLocal()
    try:
        tag = uuid4().hex[:5]
        z = _host(db, f"db{tag}-01.corp", ip=f"10.66.{int(tag[:1], 16)}.9")
        d = _host(db, f"DB{tag}-01", platform=SourcePlatform.dynatrace, instance="DT-AI", ip=z.ip)
        _alert(db, z, "CPU high")
        _alert(db, d, "Response time degraded")
        _alert(db, z, "old one", resolved=True)
        db.commit()
    finally:
        db.close()
    broker = ToolBroker(get_settings())
    r = broker.execute("get_host_alerts", {"hostname": f"db{tag}-01"})
    assert r.ok and {row["source_platform"] for row in r.rows} == {"zabbix", "dynatrace"}
    assert all(row["state"] == "active" for row in r.rows)
    assert r.meta["monitored_by"] == ["dynatrace", "zabbix"]
    r2 = broker.execute("get_host_alerts", {"hostname": f"db{tag}-01", "active_only": False})
    assert len(r2.rows) == 3
    none = broker.execute("get_host_status", {"hostname": "ghost-host-xyz"})
    assert none.ok and none.rows == [] and none.meta["resolution"] == "none"


# --- evidence pack ---------------------------------------------------------------


def test_evidence_pack_marks_a_failed_platform_unknown():
    freshness = {
        "Zabbix-DC1": {"platform": "zabbix", "status": "failed", "last_run_at": "2026-10-02T12:10:00+00:00",
                       "last_success_at": "2026-10-02T11:05:00+00:00", "error": "timeout"},
        "NNMi-Core": {"platform": "nnmi", "status": "success", "last_run_at": "2026-10-02T12:00:00+00:00",
                      "last_success_at": "2026-10-02T12:00:00+00:00", "error": None},
    }
    ok = ToolResult("get_host_status", {"hostname": "x"}, True,
                    rows=[{"record_id": "host:1", "source_platform": "nnmi", "source_instance": "NNMi-Core",
                           "hostname": "x", "status": "up"}])
    failed = ToolResult("get_capacity_risk", {}, False, error="get_capacity_risk timed out after 10s")
    pack = build_pack("is x up?", [ok, failed], freshness)
    assert len(pack.facts) == 1 and pack.facts[0]["type"] == "FACT"
    assert pack.facts[0]["as_of"] == "2026-10-02T12:00:00+00:00"
    joined = " ".join(pack.unknowns)
    assert "Zabbix data from Zabbix-DC1 is unavailable" in joined and "11:05" in joined
    assert "get_capacity_risk could not run" in joined
    assert freshness_line(freshness) == "Data as of: Zabbix 11:05, NNMi 12:00"


# --- output validator -------------------------------------------------------------


def _pack_with(hostname="web-01", pct=83.0):
    r = ToolResult("get_host_status", {"hostname": hostname}, True, rows=[{
        "record_id": "host:5", "source_platform": "zabbix", "source_instance": "Zabbix-DC1",
        "hostname": hostname, "status": "up", "cpu_pct": pct, "last_seen": "2026-10-02T12:05:00+00:00",
    }])
    return build_pack(f"is {hostname} up?", [r], {})


def test_validator_catches_fabricated_hostnames_and_numbers():
    pack = _pack_with()
    good = validate_answer("web-01 is up (Zabbix, Zabbix-DC1, as of 12:05). CPU is 83%.", pack)
    assert good.passed, good.problems
    bad = validate_answer("web-01 and db-prod-07 are up; db-prod-07 shows 97% CPU.", pack)
    assert not bad.passed
    assert any("db-prod-07" in p for p in bad.problems) and any("'97'" in p for p in bad.problems)


def test_validator_requires_empty_pack_to_be_acknowledged():
    empty = EvidencePack(question="is mw10 up?")
    assert not validate_answer("mw10 is up and healthy.", empty).passed
    assert validate_answer("SAMIX has no data for mw10 - not found on any platform.", empty).passed
    assert validate_answer("مفيش بيانات عن mw10 في SAMIX.", empty).passed


# --- gateway -------------------------------------------------------------------------


def test_disabled_ai_is_404_everywhere(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_ai", False)
    set_gateway(None)
    assert client.get("/ai").status_code == 404
    assert client.post("/api/v1/ai/ask", json={"question": "hi"}).status_code == 404
    assert client.post("/partials/ai/ask", data={"question": "hi"}).status_code == 404


def test_login_required_by_default(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "enable_ai", True)
    monkeypatch.setattr(s, "ai_require_login", True)
    set_gateway(Gateway(s, llm=FakeLLM()))
    try:
        client.cookies.clear()
        assert client.get("/ai").status_code == 401
        assert client.post("/api/v1/ai/ask", json={"question": "hi"}).status_code == 401
    finally:
        set_gateway(None)


def test_rate_limit_is_per_user():
    rl = RateLimiter(2)
    rl.check("a"); rl.check("a")
    with pytest.raises(GatewayError) as exc:
        rl.check("a")
    assert exc.value.status_code == 429
    rl.check("b")  # another user is unaffected


def test_ask_flow_end_to_end_with_fake_model(client, ai_on):
    db = SessionLocal()
    try:
        tag = uuid4().hex[:5]
        h = _host(db, f"mw{tag}-10", ip=f"10.55.{int(tag[:1], 16)}.10")
        _alert(db, h, "Interface down on eth1", sev=5)
        db.add(CollectorRun(platform="zabbix", instance="ZBX-AI", started_at=NOW, finished_at=NOW,
                            status=RunStatus.success, hosts_collected=1, alerts_collected=1))
        db.commit()
    finally:
        db.close()

    r = client.post("/api/v1/ai/ask", json={"question": f"فيه مشاكل ايه على mw{tag}-10؟"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tools"][0]["name"] == "get_host_alerts" and body["tools"][0]["rows"] == 1
    assert body["validation"]["passed"] is True
    assert body["evidence"]["facts"][0]["source_platform"] == "zabbix"
    assert "Interface down" in body["answer"] and "ZBX-AI" in body["answer"]
    assert body["freshness"].startswith("Data as of: Zabbix")
    assert body["rounds"] == 2  # one tool round, then the model said it was ready

    db = SessionLocal()
    try:
        audit = db.scalar(select(AIAudit).where(AIAudit.trace_id == body["trace_id"]))
        assert audit is not None and audit.validation == "passed" and audit.model == "fake-deterministic"
        assert audit.question.startswith("فيه")
    finally:
        db.close()

    fb = client.post("/api/v1/ai/feedback", json={"trace_id": body["trace_id"], "vote": -1, "comment": "too long"})
    assert fb.status_code == 200
    assert client.post("/api/v1/ai/feedback", json={"trace_id": "nope-nope", "vote": 1}).status_code == 404
    db = SessionLocal()
    try:
        votes = db.scalars(select(AIFeedback).where(AIFeedback.trace_id == body["trace_id"])).all()
        assert [v.vote for v in votes] == [-1] and votes[0].comment == "too long"
    finally:
        db.close()


def test_unknown_host_gets_an_honest_answer(client, ai_on):
    r = client.post("/api/v1/ai/ask", json={"question": "what problems are on ghostbox-99?"})
    assert r.status_code == 200
    body = r.json()
    assert body["evidence"]["facts"] == []
    assert body["validation"]["passed"] is True
    assert "No matching data" in body["answer"] or "no host" in body["answer"].lower()


def test_fabricated_answer_is_replaced_by_raw_evidence(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "enable_ai", True)
    monkeypatch.setattr(s, "ai_require_login", False)
    liar = FakeLLM(answer_override="core-router-77 is down with 12 critical alerts.")
    set_gateway(Gateway(s, llm=liar))
    try:
        r = client.post("/api/v1/ai/ask", json={"question": "worst alerts now?"})
        assert r.status_code == 200
        assert r.json()["validation"]["passed"] is False
        # The HTMX card shows the notice and the raw table instead of the prose.
        card = client.post("/partials/ai/ask", data={"question": "worst alerts now?"})
        assert card.status_code == 200 and "Working on it" in card.text
        trace = card.text.split("/partials/ai/progress/")[1].split('"')[0]
        for _ in range(60):
            page = client.get(f"/partials/ai/progress/{trace}")
            if "Working on it" not in page.text:
                break
            time.sleep(0.05)
        assert "failed validation" in page.text and "raw evidence" in page.text
        assert "Data as of" in page.text and trace in page.text
    finally:
        set_gateway(None)


def test_page_renders_with_model_status_and_examples(client, ai_on):
    page = client.get("/ai")
    assert page.status_code == 200
    assert "fake-deterministic" in page.text and "Ask" in page.text and "MW10" in page.text
    assert client.get("/api/v1/ai/health").json()["ok"] is True

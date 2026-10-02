from __future__ import annotations

from datetime import datetime, timezone

from app.ai_poc.gateway import AIGateway, AIUserContext, DynatraceProblemRequest


class FakeCollector:
    instance = "Dynatrace-POC"

    def collect_alerts(self):
        return [{
            "external_id": "p-1",
            "host_hostname": "MW10",
            "title": "CPU high",
            "severity_label": "High",
            "started_at": datetime.now(timezone.utc),
        }]


def user(**kwargs):
    base = {"user_id": "operator-1", "authenticated": True, "scopes": frozenset({"ai.read"}), "allowed_hosts": frozenset({"MW10"})}
    base.update(kwargs)
    return AIUserContext(**base)


def test_gateway_is_disabled_by_default_and_audits_denial():
    audit = []
    gateway = AIGateway(audit_sink=audit.append)
    result = gateway.get_dynatrace_problems(user(), DynatraceProblemRequest("MW10"), FakeCollector())
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_AUTHORIZED"
    assert audit[-1].allowed is False
    assert audit[-1].reason == "AI PoC gateway is disabled"


def test_gateway_allows_only_authenticated_scoped_host():
    audit = []
    gateway = AIGateway(enabled=True, audit_sink=audit.append)
    result = gateway.get_dynatrace_problems(user(), DynatraceProblemRequest("mw10"), FakeCollector())
    assert result.ok is True
    assert [item.source_id for item in result.items] == ["p-1"]
    assert audit[-1].allowed is True


def test_gateway_rejects_missing_scope_and_out_of_scope_host():
    gateway = AIGateway(enabled=True)
    missing_scope = gateway.get_dynatrace_problems(user(scopes=frozenset()), DynatraceProblemRequest("MW10"), FakeCollector())
    other_host = gateway.get_dynatrace_problems(user(), DynatraceProblemRequest("MW11"), FakeCollector())
    assert missing_scope.error_code == "TOOL_NOT_AUTHORIZED"
    assert other_host.error_code == "TOOL_NOT_AUTHORIZED"

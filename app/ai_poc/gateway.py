from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from app.ai_poc.contracts import ToolResult
from app.ai_poc.dynatrace_tool import get_dynatrace_problems


@dataclass(frozen=True)
class AIUserContext:
    """Identity and entitlements supplied by the host application's auth layer."""

    user_id: str
    authenticated: bool
    scopes: frozenset[str] = frozenset()
    allowed_hosts: frozenset[str] | None = None


@dataclass(frozen=True)
class DynatraceProblemRequest:
    hostname: str
    time_window_minutes: int = 60
    severity_filter: str | None = None
    max_results: int = 50


@dataclass(frozen=True)
class GatewayAuditEvent:
    actor: str
    action: str
    tool_name: str
    target: str
    allowed: bool
    reason: str
    created_at: datetime


AuditSink = Callable[[GatewayAuditEvent], None]


class AIGateway:
    """Small policy boundary for the first SAMIx AI PoC.

    This class intentionally has no LLM client and no HTTP route. It proves the
    security contract before model integration: disabled by default, one
    read-only tool, authenticated identity, explicit scope, and host scoping.
    """

    def __init__(self, *, enabled: bool = False, audit_sink: AuditSink | None = None):
        self.enabled = enabled
        self.audit_sink = audit_sink
        self.allowed_tools = frozenset({"get_dynatrace_problems"})

    def get_dynatrace_problems(
        self,
        user: AIUserContext,
        request: DynatraceProblemRequest,
        collector,
    ) -> ToolResult:
        decision = self._authorize(user, request.hostname)
        if decision is not None:
            self._audit(user, request.hostname, False, decision)
            return ToolResult(
                tool_name="get_dynatrace_problems",
                schema_version="samix.ai.dynatrace-problems.v0.1",
                source="dynatrace",
                read_only=True,
                ok=False,
                generated_at=datetime.now(timezone.utc),
                freshness_seconds=None,
                error_code="TOOL_NOT_AUTHORIZED",
                error_message=decision,
            )
        self._audit(user, request.hostname, True, "allowed")
        return get_dynatrace_problems(
            collector,
            hostname=request.hostname,
            time_window_minutes=request.time_window_minutes,
            severity_filter=request.severity_filter,
            max_results=request.max_results,
        )

    def _authorize(self, user: AIUserContext, hostname: str) -> str | None:
        if not self.enabled:
            return "AI PoC gateway is disabled"
        if not user.authenticated or not user.user_id.strip():
            return "authenticated user is required"
        if "ai.read" not in user.scopes:
            return "missing ai.read scope"
        if not hostname.strip():
            return "hostname is required"
        if user.allowed_hosts is not None and hostname.casefold() not in {h.casefold() for h in user.allowed_hosts}:
            return "user is not authorized for this host"
        return None

    def _audit(self, user: AIUserContext, hostname: str, allowed: bool, reason: str) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink(
            GatewayAuditEvent(
                actor=user.user_id or "anonymous",
                action="ai_tool_call",
                tool_name="get_dynatrace_problems",
                target=hostname[:255],
                allowed=allowed,
                reason=reason,
                created_at=datetime.now(timezone.utc),
            )
        )

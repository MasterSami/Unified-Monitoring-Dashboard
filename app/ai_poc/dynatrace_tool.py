from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.ai_poc.contracts import ProblemEvidence, ToolResult

TOOL_NAME = "get_dynatrace_problems"
SCHEMA_VERSION = "samix.ai.dynatrace-problems.v0.1"


def get_dynatrace_problems(
    collector: Any,
    *,
    hostname: str,
    time_window_minutes: int = 60,
    severity_filter: str | None = None,
    max_results: int = 50,
    now: datetime | None = None,
) -> ToolResult:
    """Read active Dynatrace problems for one host from an existing collector.

    The adapter is deliberately dependency-injected: it does not open a new
    network client and never receives credentials. The collector remains the
    only component allowed to communicate with Dynatrace.
    """
    generated_at = now or datetime.now(timezone.utc)
    safe_host = hostname.strip()
    if not safe_host:
        return _error(generated_at, "INVALID_HOST", "hostname is required")
    if not 1 <= time_window_minutes <= 7 * 24 * 60:
        return _error(generated_at, "INVALID_WINDOW", "time_window_minutes must be between 1 and 10080")
    if not 1 <= max_results <= 500:
        return _error(generated_at, "INVALID_LIMIT", "max_results must be between 1 and 500")

    try:
        rows = collector.collect_alerts()
    except Exception as exc:  # noqa: BLE001 - source failures become explicit evidence
        return _error(generated_at, "SOURCE_UNAVAILABLE", f"Dynatrace could not be queried: {str(exc)[:240]}")

    cutoff = generated_at - timedelta(minutes=time_window_minutes)
    wanted_severity = severity_filter.strip().lower() if severity_filter else None
    matches: list[ProblemEvidence] = []
    for row in rows or []:
        row_host = str(row.get("host_hostname") or "").strip()
        started = _as_aware(row.get("started_at"))
        if row_host.casefold() != safe_host.casefold():
            continue
        if started is not None and started < cutoff:
            continue
        severity = str(row.get("severity_label") or "unknown")
        if wanted_severity and severity.casefold() != wanted_severity:
            continue
        source_id = str(row.get("external_id") or "").strip()
        if not source_id:
            continue
        matches.append(
            ProblemEvidence(
                source_id=source_id,
                hostname=row_host or None,
                title=str(row.get("title") or ""),
                severity=severity,
                started_at=started,
                source_instance=str(getattr(collector, "instance", "") or ""),
                source_url=None,
            )
        )

    matches.sort(key=lambda item: item.started_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    truncated = len(matches) > max_results
    matches = matches[:max_results]
    return ToolResult(
        tool_name=TOOL_NAME,
        schema_version=SCHEMA_VERSION,
        source="dynatrace",
        read_only=True,
        ok=True,
        generated_at=generated_at,
        freshness_seconds=0,
        items=matches,
        truncated=truncated,
    )


def _as_aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _error(generated_at: datetime, code: str, message: str) -> ToolResult:
    return ToolResult(
        tool_name=TOOL_NAME,
        schema_version=SCHEMA_VERSION,
        source="dynatrace",
        read_only=True,
        ok=False,
        generated_at=generated_at,
        freshness_seconds=None,
        error_code=code,
        error_message=message,
    )

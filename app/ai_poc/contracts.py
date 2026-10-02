from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class ProblemEvidence:
    """Safe, model-facing representation of one Dynatrace problem.

    Raw API payloads and credentials are deliberately excluded. ``source_id``
    and ``source_url`` let SAMIx render an auditable reference without making
    the LLM responsible for constructing links.
    """

    source_id: str
    hostname: str | None
    title: str
    severity: str
    started_at: datetime | None
    source_instance: str
    source_url: str | None = None
    evidence_type: Literal["FACT"] = "FACT"


@dataclass(frozen=True)
class ToolResult:
    """Versioned read-only tool response passed to a future orchestrator."""

    tool_name: str
    schema_version: str
    source: str
    read_only: bool
    ok: bool
    generated_at: datetime
    freshness_seconds: int | None
    items: list[ProblemEvidence] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    truncated: bool = False

    def to_model_context(self) -> dict[str, Any]:
        """Return only structured evidence suitable for a future LLM prompt."""
        return {
            "tool": self.tool_name,
            "schema_version": self.schema_version,
            "source": self.source,
            "read_only": self.read_only,
            "ok": self.ok,
            "generated_at": self.generated_at.isoformat(),
            "freshness_seconds": self.freshness_seconds,
            "items": [
                {
                    "evidence_type": item.evidence_type,
                    "source_id": item.source_id,
                    "hostname": item.hostname,
                    "title": item.title,
                    "severity": item.severity,
                    "started_at": item.started_at.isoformat() if item.started_at else None,
                    "source_instance": item.source_instance,
                    "source_url": item.source_url,
                }
                for item in self.items
            ],
            "error_code": self.error_code,
            "error_message": self.error_message,
            "truncated": self.truncated,
        }

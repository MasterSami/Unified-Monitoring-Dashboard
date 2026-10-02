"""Role 5 - the audit trail.

Every question becomes one ``ai_audit`` row: who asked what, which tools ran
with which arguments and how many rows they returned, which model answered,
how long it took and whether the answer passed validation. No secrets: the
row holds the question as typed and tool arguments (hostnames, filters),
never credentials or raw platform payloads.

Thumbs up / down land in ``ai_feedback`` keyed by the same trace_id - the
evaluation set for choosing and tuning the model later.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import SessionLocal, write_lock
from app.models import AIAudit, AIFeedback

logger = logging.getLogger("ai.audit")


def record_audit(
    *,
    trace_id: str,
    user: str,
    question: str,
    tools: list[dict[str, Any]],
    rounds: int,
    model: str,
    latency_ms: int,
    validation: str,
    answer_chars: int,
    error: str | None = None,
) -> None:
    """Never raises: a failed audit write is logged, the answer still goes out."""
    try:
        with write_lock:
            db = SessionLocal()
            try:
                db.add(AIAudit(
                    trace_id=trace_id, user=user[:128], question=question[:4000],
                    tools_json=tools, rounds=rounds, model=model[:128], latency_ms=latency_ms,
                    validation=validation[:16], answer_chars=answer_chars,
                    error=(error or None) and error[:1000],
                ))
                db.commit()
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        logger.exception("ai audit write failed for %s", trace_id)


def record_feedback(*, trace_id: str, vote: int, comment: str | None, user: str) -> bool:
    """One vote row per click. Returns False only if the trace is unknown."""
    with write_lock:
        db = SessionLocal()
        try:
            known = db.query(AIAudit.id).filter(AIAudit.trace_id == trace_id).first()
            if known is None:
                return False
            db.add(AIFeedback(trace_id=trace_id, vote=1 if vote > 0 else -1,
                              comment=(comment or "").strip()[:2000] or None, user=user[:128]))
            db.commit()
            return True
        finally:
            db.close()

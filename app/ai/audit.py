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

from sqlalchemy import func

from app.db import SessionLocal, write_lock
from app.models import AIAudit, AICorrection, AIFeedback

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


def save_correction(
    *,
    question_pattern: str,
    keywords: str,
    correct_answer: str,
    data_source: str | None = None,
    root_cause_hint: str | None = None,
    trace_id: str | None = None,
    created_by: str = "",
) -> int:
    with write_lock:
        db = SessionLocal()
        try:
            row = AICorrection(
                question_pattern=question_pattern[:500],
                keywords=keywords[:1000],
                correct_answer=correct_answer[:4000],
                data_source=(data_source or "")[:1000] or None,
                root_cause_hint=(root_cause_hint or "")[:2000] or None,
                trace_id=trace_id,
                created_by=created_by[:128],
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.id
        finally:
            db.close()


def find_corrections(question: str, limit: int = 5) -> list[dict]:
    """Return corrections whose keywords overlap with the question."""
    db = SessionLocal()
    try:
        rows = db.query(AICorrection).filter(AICorrection.enabled == True).all()  # noqa: E712
        q_lower = question.lower()
        scored: list[tuple[int, AICorrection]] = []
        for r in rows:
            kws = [k.strip().lower() for k in r.keywords.split(",") if k.strip()]
            hits = sum(1 for k in kws if k in q_lower)
            if hits > 0:
                scored.append((hits, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for _, r in scored[:limit]:
            d = {"correct_answer": r.correct_answer, "question_pattern": r.question_pattern}
            if r.data_source:
                d["data_source"] = r.data_source
            if r.root_cause_hint:
                d["root_cause_hint"] = r.root_cause_hint
            result.append(d)
        return result
    finally:
        db.close()


def list_corrections() -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(AICorrection).order_by(AICorrection.created_at.desc()).all()
        return [
            {
                "id": r.id, "question_pattern": r.question_pattern,
                "keywords": r.keywords, "correct_answer": r.correct_answer,
                "data_source": r.data_source, "root_cause_hint": r.root_cause_hint,
                "enabled": r.enabled, "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


def delete_correction(correction_id: int) -> bool:
    with write_lock:
        db = SessionLocal()
        try:
            row = db.query(AICorrection).filter(AICorrection.id == correction_id).first()
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True
        finally:
            db.close()


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

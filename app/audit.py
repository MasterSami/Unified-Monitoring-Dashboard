"""Audit logging — Correlation Phase 6.

One small, explicit helper so every incident-affecting write action a human
takes (feedback, merge, split) leaves a durable trail: who, what, on what,
and when. Deliberately never passed a raw request body — callers hand it a
short, hand-picked ``details`` dict, so no source credential or full payload
can ever land in the audit table (task section 14: "no sensitive credentials
in logs").
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(db: Session, *, actor: str, action: str, target: str, details: dict | None = None) -> AuditLog:
    row = AuditLog(actor=actor, action=action, target=target, details=details or {})
    db.add(row)
    db.flush()
    return row

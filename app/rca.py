"""Dynatrace Root Cause Analysis — fetch, store and query.

Uses the Davis AI evidence chain from /api/v2/problems/{id}?fields=+evidenceDetails,+impactAnalysis
to build an RCA timeline: which entity caused the problem, what evidence supports that,
and what the blast radius looks like.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors import build_collector
from app.collectors.dynatrace import DynatraceCollector
from app.config import Settings, get_settings
from app.db import SessionLocal, write_lock
from app.models import Alert, RCAEvidence, RootCauseAnalysis
from app.servers import load_servers

logger = logging.getLogger("rca")


# ---------------------------------------------------------------------------
# Collector lookup
# ---------------------------------------------------------------------------


def _get_dt_collector(instance: str = "all", settings: Settings | None = None) -> DynatraceCollector | None:
    """Return a DynatraceCollector for the given instance name (or the first one)."""
    settings = settings or get_settings()
    for cfg in load_servers(settings):
        if cfg.platform != "dynatrace":
            continue
        if instance != "all" and cfg.name != instance:
            continue
        collector = build_collector(cfg, settings)
        if isinstance(collector, DynatraceCollector):
            return collector
    return None


# ---------------------------------------------------------------------------
# Fetch and store
# ---------------------------------------------------------------------------


def fetch_and_store_rca(
    db: Session,
    collector: DynatraceCollector,
    problem_id: str,
    alert_id: int | None = None,
    instance: str = "",
) -> RootCauseAnalysis | None:
    """Fetch RCA for a Dynatrace problem and persist it."""
    data = collector.fetch_problem_rca(problem_id)
    if not data:
        return None

    rc_entity = data.get("rootCauseEntity") or {}
    rc_entity_id = rc_entity.get("entityId") or {}
    rc_entity_name = rc_entity.get("name") or ""
    rc_entity_type = ""
    if isinstance(rc_entity_id, dict):
        rc_entity_id = rc_entity_id.get("id", "")
    if rc_entity_id and "-" in str(rc_entity_id):
        rc_entity_type = str(rc_entity_id).split("-")[0]

    # Build the entity name from entity details if not in the problem response
    if rc_entity_id and not rc_entity_name:
        entity_detail = collector.fetch_entity_details(str(rc_entity_id))
        if entity_detail:
            rc_entity_name = entity_detail.get("displayName", "")
            rc_entity_type = rc_entity_type or entity_detail.get("type", "")

    impact = data.get("impactAnalysis") or {}
    evidence_details = data.get("evidenceDetails") or {}
    evidence_list = evidence_details.get("details") or []

    summary_parts = [data.get("title", "")]
    if rc_entity_name:
        summary_parts.append(f"Root cause: {rc_entity_name}")
    if impact.get("impacts"):
        n_impacted = len(impact["impacts"])
        summary_parts.append(f"{n_impacted} impacted entit{'y' if n_impacted == 1 else 'ies'}")
    summary = " | ".join(p for p in summary_parts if p)

    with write_lock:
        existing = db.execute(
            select(RootCauseAnalysis).where(RootCauseAnalysis.problem_id == problem_id)
        ).scalar_one_or_none()
        if existing:
            existing.root_cause_entity_id = str(rc_entity_id) if rc_entity_id else None
            existing.root_cause_entity_name = rc_entity_name or None
            existing.root_cause_entity_type = rc_entity_type or None
            existing.summary = summary[:4000]
            existing.impact_json = impact
            existing.status = data.get("status", "OPEN").lower()
            db.commit()
            db.refresh(existing)
            rca = existing
        else:
            rca = RootCauseAnalysis(
                alert_id=alert_id,
                problem_id=problem_id,
                source_instance=instance or collector.instance,
                root_cause_entity_id=str(rc_entity_id) if rc_entity_id else None,
                root_cause_entity_name=rc_entity_name or None,
                root_cause_entity_type=rc_entity_type or None,
                summary=summary[:4000],
                impact_json=impact,
                status=data.get("status", "OPEN").lower(),
            )
            db.add(rca)
            db.commit()
            db.refresh(rca)

        # Upsert evidence rows (replace on re-investigation)
        db.execute(
            RCAEvidence.__table__.delete().where(RCAEvidence.rca_id == rca.id)
        )
        for ev in evidence_list:
            ev_entity = ev.get("entity") or {}
            start_ms = ev.get("startTime")
            start_dt = None
            if start_ms and isinstance(start_ms, (int, float)):
                start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            raw_eid = ev_entity.get("entityId") if isinstance(ev_entity, dict) else None
            if isinstance(raw_eid, dict):
                raw_eid = raw_eid.get("id", "")
            db.add(RCAEvidence(
                rca_id=rca.id,
                evidence_type=ev.get("evidenceType", ""),
                display_name=ev.get("displayName", "")[:1000],
                entity_id=str(raw_eid) if raw_eid else None,
                entity_name=ev_entity.get("name") if isinstance(ev_entity, dict) else None,
                is_root_cause_relevant=bool(ev.get("rootCauseRelevant")),
                start_time=start_dt,
                raw_data=ev,
            ))
        db.commit()

    logger.info("RCA stored for problem %s (rca_id=%s, %d evidence rows)",
                problem_id, rca.id, len(evidence_list))
    return rca


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _rca_to_dict(rca: RootCauseAnalysis, evidence: list[RCAEvidence]) -> dict[str, Any]:
    """Serialize an RCA + its evidence chain to a dict."""
    return {
        "id": rca.id,
        "alert_id": rca.alert_id,
        "problem_id": rca.problem_id,
        "source_instance": rca.source_instance,
        "root_cause": {
            "entity_id": rca.root_cause_entity_id,
            "entity_name": rca.root_cause_entity_name,
            "entity_type": rca.root_cause_entity_type,
        },
        "summary": rca.summary,
        "impact": rca.impact_json,
        "status": rca.status,
        "created_at": rca.created_at.isoformat() if rca.created_at else None,
        "updated_at": rca.updated_at.isoformat() if rca.updated_at else None,
        "evidence": [
            {
                "id": ev.id,
                "evidence_type": ev.evidence_type,
                "display_name": ev.display_name,
                "entity_id": ev.entity_id,
                "entity_name": ev.entity_name,
                "is_root_cause_relevant": ev.is_root_cause_relevant,
                "start_time": ev.start_time.isoformat() if ev.start_time else None,
            }
            for ev in evidence
        ],
    }


def fetch_and_store_rca_by_problem_id(
    db: Session, problem_id: str, instance: str = "all"
) -> RootCauseAnalysis | None:
    """Convenience: find a DT collector and fetch+store RCA in one call."""
    collector = _get_dt_collector(instance)
    if not collector:
        return None
    alert = db.execute(
        select(Alert).where(Alert.external_id == problem_id, Alert.source_platform == "dynatrace")
    ).scalar_one_or_none()
    return fetch_and_store_rca(
        db, collector, problem_id,
        alert_id=alert.id if alert else None,
        instance=instance,
    )


def get_rca_for_alert(db: Session, alert_id: int) -> dict[str, Any] | None:
    """Load the latest RCA for a given alert ID."""
    rca = db.execute(
        select(RootCauseAnalysis)
        .where(RootCauseAnalysis.alert_id == alert_id)
        .order_by(RootCauseAnalysis.created_at.desc())
    ).scalar_one_or_none()
    if not rca:
        return None
    evidence = list(db.scalars(
        select(RCAEvidence).where(RCAEvidence.rca_id == rca.id).order_by(RCAEvidence.id)
    ).all())
    return _rca_to_dict(rca, evidence)


def get_rca_for_problem(db: Session, problem_id: str) -> dict[str, Any] | None:
    """Load the latest RCA for a Dynatrace problem ID."""
    rca = db.execute(
        select(RootCauseAnalysis)
        .where(RootCauseAnalysis.problem_id == problem_id)
        .order_by(RootCauseAnalysis.created_at.desc())
    ).scalar_one_or_none()
    if not rca:
        return None
    evidence = list(db.scalars(
        select(RCAEvidence).where(RCAEvidence.rca_id == rca.id).order_by(RCAEvidence.id)
    ).all())
    return _rca_to_dict(rca, evidence)


def fetch_entity_health(instance: str, entity_id: str, entity_type: str) -> dict[str, Any]:
    """Get current metrics for a Dynatrace entity."""
    collector = _get_dt_collector(instance)
    if not collector:
        return {"error": "No Dynatrace instance available"}
    metrics = collector.fetch_entity_metrics(entity_id, entity_type)
    details = collector.fetch_entity_details(entity_id) or {}
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "display_name": details.get("displayName", ""),
        "properties": details.get("properties", {}),
        "metrics": metrics,
    }

"""Database engine, session factory, and declarative base.

Uses SQLAlchemy 2.x. The engine is built from ``DATABASE_URL`` so the exact
same code runs against SQLite (POC) and PostgreSQL (server deploy).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# SQLite needs a special connect arg when used across threads (the scheduler
# runs in a background thread). PostgreSQL ignores this branch entirely.
_connect_args: dict[str, object] = {}
_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite:
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    # pool_pre_ping issues a `SELECT 1` on every checkout to detect connections
    # a network or a server restart dropped. That is worth it for PostgreSQL and
    # pointless for a local SQLite file, where the connection cannot go stale
    # over the wire — so skip the extra round trip per request there.
    pool_pre_ping=not _is_sqlite,
    connect_args=_connect_args,
)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """Make SQLite safe for a web app + a background collector thread.

        Default SQLite journaling takes a database-wide write lock and readers
        get an immediate ``database is locked`` error (surfacing as HTTP 500)
        whenever a collection run is writing — which our longer, write-heavy
        runs (event history backfill, per-host metrics, disk breakdown) made
        frequent. WAL lets readers and the writer run concurrently, and a
        ``busy_timeout`` makes the rare writer-vs-writer case wait briefly
        instead of failing. ``synchronous=NORMAL`` is the WAL-recommended,
        crash-safe durability level.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=8000")  # ms
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


#: Columns added after the first release, applied to existing DBs on startup.
#: (name -> SQL type) — kept in sync with the ORM models. New tables are handled
#: by create_all; only *added columns on existing tables* need this.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "hosts": {
        "cpu_pct": "FLOAT",
        "mem_pct": "FLOAT",
        "disk_pct": "FLOAT",
        "metrics": "JSON",
        "owner": "VARCHAR(255)",
        "owner_email": "VARCHAR(255)",
        "agent_deployed": "BOOLEAN",
        "ip_all": "VARCHAR(512)",
        # Correlation Phase 1: entity resolution (app/entity_resolution.py).
        "fqdn": "VARCHAR(255)",
        "cmdb_id": "VARCHAR(128)",
        "entity_id": "INTEGER",
        "resolution_method": "VARCHAR(32)",
        "resolution_confidence": "FLOAT",
    },
    "alerts": {
        "state": "VARCHAR(64)",
        "dedup_key": "VARCHAR(255)",
        "metric_missing": "BOOLEAN",
        "monitor_name": "VARCHAR(512)",
        "host_external_id": "VARCHAR(128)",
        # Correlation Phase 1: canonical event fields (app/models.py Alert,
        # app/canonical_event.py). See those modules for what populates each.
        "status": "VARCHAR(24)",
        "last_seen": "DATETIME",
        "original_severity": "VARCHAR(32)",
        "original_description": "VARCHAR(1024)",
        "payload_ref": "VARCHAR(128)",
        "host_ip": "VARCHAR(64)",
        "fqdn": "VARCHAR(255)",
        "host_group": "VARCHAR(255)",
        "host_owner": "VARCHAR(255)",
        "entity_id": "INTEGER",
        "entity_type": "VARCHAR(32)",
        "event_type": "VARCHAR(32)",
        "problem_type": "VARCHAR(128)",
        "tags": "JSON",
        "application_id": "INTEGER",
        "application_name": "VARCHAR(255)",
        "service_id": "INTEGER",
        "service_name": "VARCHAR(255)",
        "api_id": "INTEGER",
        "api_name": "VARCHAR(255)",
        "endpoint": "VARCHAR(512)",
        "http_method": "VARCHAR(16)",
        "database_id": "INTEGER",
        "database_name": "VARCHAR(255)",
        "network_device_id": "INTEGER",
        "network_device_name": "VARCHAR(255)",
        "metric_name": "VARCHAR(255)",
        "metric_value": "FLOAT",
        "metric_unit": "VARCHAR(32)",
        "threshold": "FLOAT",
        "environment": "VARCHAR(64)",
        "location": "VARCHAR(128)",
        "business_service": "VARCHAR(255)",
        "trace_id": "VARCHAR(64)",
        "span_id": "VARCHAR(64)",
        "parent_span_id": "VARCHAR(64)",
        # Correlation Phase 2: deduplication (app/fingerprint.py, app/dedup.py).
        "normalized_problem_type": "VARCHAR(64)",
        "fingerprint": "VARCHAR(512)",
        "logical_event_id": "INTEGER",
        # Correlation Phase 5: recovery timestamp + trace/span request context
        # (app/models.py Alert, app/trace_ingest.py).
        "resolved_at": "DATETIME",
        "http_status_code": "INTEGER",
        "duration_ms": "FLOAT",
        "is_error": "BOOLEAN",
        "db_calls": "JSON",
        "external_calls": "JSON",
    },
    "incidents": {
        # Correlation Phase 6: the Incidents UI's "Sources" column/filter
        # (app/models.py Incident, app/incident_engine.py).
        "sources": "JSON",
    },
    "correlation_evidence": {
        # Correlation Phase 7 (AI-readiness): structured from/to entity +
        # which rule produced this row (app/models.py CorrelationEvidence,
        # app/correlation_engine.py).
        "from_entity_id": "INTEGER",
        "to_entity_id": "INTEGER",
        "rule_id": "VARCHAR(64)",
    },
    "incident_feedback": {
        # Correlation Phase 7: which entity the operator confirms is the
        # real root cause (app/models.py IncidentFeedback).
        "confirmed_root_cause_entity_id": "INTEGER",
    },
}


#: Indexes added after the first release. ``CREATE INDEX IF NOT EXISTS`` is
#: supported by both SQLite and PostgreSQL, so this is safe on every startup.
#: (index_name, table, "(col, ...)")
_ADDED_INDEXES: list[tuple[str, str, str]] = [
    # Shared-devices page: GROUP BY ip + COUNT(DISTINCT source_instance).
    ("ix_hosts_ip_instance", "hosts", "(ip, source_instance)"),
    # Alerts date-range filter + severity ordering.
    ("ix_alerts_started_at", "alerts", "(started_at)"),
    ("ix_alerts_resolved_sev", "alerts", "(resolved, severity_int)"),
    # Alerts list: filter by resolved, order by severity desc, started desc.
    ("ix_alerts_resolved_sev_started", "alerts", "(resolved, severity_int, started_at)"),
    # Owner lookup for Escalate: hosts by hostname.
    ("ix_hosts_hostname", "hosts", "(hostname)"),
    # Hosts last_seen range filter on the Capacity page.
    ("ix_hosts_last_seen", "hosts", "(last_seen)"),
    # Collector health: latest run / latest success per instance (batched).
    ("ix_collector_runs_instance_id", "collector_runs", "(instance, id)"),
    ("ix_collector_runs_status_instance_id", "collector_runs", "(status, instance, id)"),
    # Agents page: the per-agent alert rollup groups by (instance, hostname)
    # over unresolved alerts, and the drill-down is a point lookup on the same
    # three columns. Without this both scan the whole alerts table — which now
    # includes the 30-day resolved backfill, so it is the largest table we have.
    ("ix_alerts_resolved_instance_host", "alerts",
     "(resolved, source_instance, host_hostname)"),
    # Capacity/Agents default ordering: filter by platform+instance, sort by name.
    ("ix_hosts_platform_instance_hostname", "hosts",
     "(source_platform, source_instance, hostname)"),
    # Overview agent rollup: GROUP BY (instance, platform, status).
    ("ix_hosts_instance_platform_status", "hosts",
     "(source_instance, source_platform, status)"),
    # Capacity group filter.
    ("ix_hosts_group_name", "hosts", "(group_name)"),
    # Forecasting reads one whole series at a time: every sample for a
    # (host, metric, subject) in date order. Without this the nightly job
    # scans the largest table in the schema once per series.
    ("ix_caphist_series_time", "capacity_history",
     "(host_id, metric_kind, subject, sampled_at)"),
    # Retention pruning, and the "have we sampled this host recently?" probe
    # the collector runs once per instance per poll.
    ("ix_caphist_sampled_at", "capacity_history", "(sampled_at)"),
    ("ix_caphist_host_sampled", "capacity_history", "(host_id, sampled_at)"),
    # /forecast orders by days_to_threshold_90 within a classification.
    ("ix_capfc_class_eta", "capacity_forecast",
     "(classification, days_to_threshold_90)"),
    ("ix_capfc_host", "capacity_forecast", "(host_id)"),
    # Correlation Phase 1: entity resolution + /events lookups (task's own
    # "avoid unnecessary full-table scans" list — source_event_id and source
    # are already covered by uq_alert_platform_instance_external / the
    # existing source_platform index).
    ("ix_hosts_entity_id", "hosts", "(entity_id)"),
    ("ix_alerts_entity_id", "alerts", "(entity_id)"),
    ("ix_alerts_host_ip", "alerts", "(host_ip)"),
    ("ix_alerts_service_id", "alerts", "(service_id)"),
    ("ix_alerts_application_id", "alerts", "(application_id)"),
    ("ix_alerts_trace_id", "alerts", "(trace_id)"),
    # started_at (the event's "timestamp") is already indexed as
    # ix_alerts_started_at above.
    ("ix_hosts_cmdb_id", "hosts", "(cmdb_id)"),
    # Correlation Phase 2: deduplication (app/dedup.py). fingerprint is the
    # batch-prefetch lookup key on both tables; logical_event_id backs the
    # /logical-events/{id}/occurrences query and the per-run aggregate recompute.
    ("ix_alerts_fingerprint", "alerts", "(fingerprint)"),
    ("ix_alerts_logical_event_id", "alerts", "(logical_event_id)"),
    ("ix_logical_events_fingerprint", "logical_events", "(fingerprint)"),
    ("ix_logical_events_entity_id", "logical_events", "(entity_id)"),
    ("ix_logical_events_status", "logical_events", "(status)"),
    ("ix_logical_events_last_seen", "logical_events", "(last_seen)"),
    # Correlation Phase 3: dependency graph traversal (app/dependency_graph.py)
    # walks from_entity_id (upstream) and to_entity_id (downstream) for one
    # entity at a time, filtered by relationship_type — this is the composite
    # each traversal step actually needs, not just the single-column indexes
    # already implied by index=True on each column.
    ("ix_entrel_from_type", "entity_relationships", "(from_entity_id, relationship_type)"),
    ("ix_entrel_to_type", "entity_relationships", "(to_entity_id, relationship_type)"),
    # Correlation Phase 4: candidate-pair lookup (app/correlation_engine.py)
    # and evidence retrieval for one correlation at a time.
    ("ix_correlation_evidence_correlation", "correlation_evidence", "(correlation_id)"),
    # Correlation Phase 5: trace ingest sibling-span lookup (app/trace_ingest.py
    # reads every span sharing a trace_id/parent_span_id to build db_calls/
    # external_calls and the timeline), and the recovery-timeline scan over
    # resolved_at. (Incident.status is already index=True on the model, so
    # create_all covers it — no separate entry needed here.)
    ("ix_alerts_trace_parent", "alerts", "(trace_id, parent_span_id)"),
    ("ix_alerts_resolved_at", "alerts", "(resolved_at)"),
    # Correlation Phase 6: the Incident list page's default view (status
    # filter, most-recently-touched first) and its business_service filter.
    ("ix_incidents_status_last_update", "incidents", "(status, last_update)"),
    ("ix_incidents_business_service", "incidents", "(business_service)"),
    # Correlation Phase 7 (AI-readiness): a future similarity/pattern pass
    # over historical evidence needs to query by entity pair or by which
    # rule fired, cheaply, across the whole table — these are ADDED columns
    # on an existing table, so index=True on the model alone (create_all
    # only) does not create them; same as every other added-column index
    # above. Named to match SQLAlchemy's own auto-generated index=True name
    # (ix_<table>_<column>) so a fresh install's create_all and this
    # CREATE INDEX IF NOT EXISTS agree instead of creating two indexes.
    ("ix_correlation_evidence_from_entity_id", "correlation_evidence", "(from_entity_id)"),
    ("ix_correlation_evidence_to_entity_id", "correlation_evidence", "(to_entity_id)"),
    ("ix_correlation_evidence_rule_id", "correlation_evidence", "(rule_id)"),
]


def _ensure_indexes() -> None:
    """Create any missing helper indexes (idempotent)."""
    with engine.begin() as conn:
        for name, table, cols in _ADDED_INDEXES:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}"))


def _ensure_columns() -> None:
    """Add any missing columns to existing tables (tiny forward-only migration).

    ``create_all`` never alters existing tables, so a schema that grew a column
    (e.g. the Capacity metrics) would break queries against an older DB. Both
    SQLite and PostgreSQL support ``ALTER TABLE ... ADD COLUMN``; we only add
    columns that are absent, so this is safe to run on every startup.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all will build it fresh with all columns
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in present:
                    conn.execute(
                        text(f'ALTER TABLE {table} ADD COLUMN {name} {sql_type}')
                    )


#: Platform values renamed after rows had already been written. Applied on
#: startup so an existing database follows the rename instead of orphaning its
#: rows under a value the enum no longer has.
_RENAMED_PLATFORMS: list[tuple[str, str]] = [
    ("huawei", "digitalview"),
]


def _rename_platforms() -> None:
    """Migrate rows written under an old platform name (idempotent)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for old, new in _RENAMED_PLATFORMS:
            for table, column in (
                ("hosts", "source_platform"),
                ("alerts", "source_platform"),
                ("collector_runs", "platform"),
            ):
                if table not in tables:
                    continue
                conn.execute(
                    text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                    {"new": new, "old": old},
                )


def init_db() -> None:
    """Create all tables, then apply small column migrations. Idempotent."""
    # Import models so they register with the metadata before create_all.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_indexes()
    _rename_platforms()

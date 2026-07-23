"""Abstract base collector (one instance per configured server).

Each concrete collector is constructed with a :class:`~app.servers.ServerConfig`
describing a single instance (e.g. one Zabbix server). It implements
:meth:`collect_hosts` and :meth:`collect_alerts`, returning lists of *normalized*
dicts. :meth:`run` orchestrates timing, upserting, and recording a
:class:`~app.models.CollectorRun` row. A failure in one instance is fully
contained: it is logged, recorded as a failed run, and never propagated to
callers or other collectors.
"""

from __future__ import annotations

import abc
import logging
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CollectorRun, RunStatus, SourcePlatform
from app.normalizer import upsert_alerts, upsert_hosts
from app.servers import ServerConfig


class CollectorError(Exception):
    """Raised for recoverable, collector-specific failures."""


class BaseCollector(abc.ABC):
    """Abstract collector for a single monitoring instance."""

    #: Platform name; concrete subclasses must set this.
    name: str = "base"
    platform: SourcePlatform

    def __init__(self, config: ServerConfig, settings: Settings) -> None:
        self.config = config
        self.settings = settings
        #: Unique instance name (e.g. "Zabbix-34"); keys everything downstream.
        self.instance = config.name
        self.logger = logging.getLogger(f"collector.{self.name}.{self.instance}")
        #: Free-form note surfaced in the UI (e.g. proxy summary or a warning).
        self.notes: str | None = None

    @property
    def verify_tls(self) -> bool:
        """Effective TLS verification (per-instance override or global)."""
        if self.config.verify_tls is not None:
            return self.config.verify_tls
        return self.settings.tls_verify

    # --- HTTP helper --------------------------------------------------------

    def _client(self, *, timeout: float = 30.0, **kwargs: object) -> httpx.Client:
        """Build a configured httpx client (TLS + timeout honoring settings)."""
        return httpx.Client(
            verify=self.verify_tls,
            timeout=timeout,
            **kwargs,  # type: ignore[arg-type]
        )

    def _request_with_retries(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        retries: int = 2,
        **kwargs: object,
    ) -> httpx.Response:
        """Issue an HTTP request with simple exponential backoff.

        Retries transport errors and 5xx responses up to ``retries`` times.
        4xx responses are returned so callers can handle them (e.g. a 403).
        """
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = client.request(method, url, **kwargs)  # type: ignore[arg-type]
                if resp.status_code >= 500 and attempt < retries:
                    raise CollectorError(f"server error {resp.status_code}")
                return resp
            except (httpx.TransportError, CollectorError) as exc:
                last_exc = exc
                if attempt < retries:
                    backoff = 2**attempt
                    self.logger.warning(
                        "request to %s failed (attempt %d/%d): %s; retry in %ds",
                        url,
                        attempt + 1,
                        retries + 1,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    # --- Contract -----------------------------------------------------------

    @abc.abstractmethod
    def collect_hosts(self) -> list[dict]:
        """Return normalized host dicts. Must raise on unrecoverable failure."""

    @abc.abstractmethod
    def collect_alerts(self) -> list[dict]:
        """Return normalized alert dicts. Must raise on unrecoverable failure."""

    # --- Orchestration ------------------------------------------------------

    def run(self, db: Session) -> CollectorRun:
        """Execute a full collection cycle and record the outcome.

        Never raises: any exception is caught, logged, and persisted on the
        :class:`CollectorRun` row so one instance's failure cannot affect
        others or the web UI.
        """
        self.notes = None
        started = datetime.now(timezone.utc)
        try:
            self.logger.info("starting collection")
            hosts = self.collect_hosts()
            alerts = self.collect_alerts()

            host_count = upsert_hosts(db, self.platform, hosts, self.instance)
            alert_count = upsert_alerts(db, self.platform, alerts, self.instance)

            run = CollectorRun(
                platform=self.name,
                instance=self.instance,
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                status=RunStatus.success,
                items_collected=host_count + alert_count,
                hosts_collected=host_count,
                alerts_collected=alert_count,
                error_message=self.notes,
            )
            db.add(run)
            db.commit()
            self.logger.info(
                "collection complete: %d hosts, %d alerts", host_count, alert_count
            )
        except Exception as exc:  # noqa: BLE001 — contained by design
            db.rollback()
            run = CollectorRun(
                platform=self.name,
                instance=self.instance,
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                status=RunStatus.failed,
                items_collected=0,
                error_message=str(exc)[:2048],
            )
            db.add(run)
            db.commit()
            self.logger.exception("collection failed: %s", exc)

        return run

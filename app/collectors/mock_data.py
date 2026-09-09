"""Realistic fake data for ``MOCK_MODE`` (per-instance).

Each function takes the instance name so multiple instances of the same
platform produce distinct, non-colliding records (external ids and hostnames
are namespaced by instance). Returned dicts are already in the normalized shape
consumed by the normalizer, so mock and live paths converge at the same upsert.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app.models import HostStatus

def _now() -> datetime:
    """The fixtures' "now" — the real clock, not a frozen timestamp.

    This used to be a constant, which meant every mock host's ``last_seen`` and
    every mock alert's ``started_at`` drifted further into the past with each
    day after the date it was written: an alert meant to read "5 minutes ago"
    eventually rendered as seven weeks old, and hosts that are notionally up
    looked long abandoned. Anchoring to the current time keeps the demo saying
    what it means.
    """
    return datetime.now(timezone.utc)


def _ago(minutes: int) -> datetime:
    return _now() - timedelta(minutes=minutes)


def _slug(instance: str) -> str:
    return instance.lower().replace(" ", "-")


# --- Host templates (hostname suffix, ip tail, status, group) --------------

_ZABBIX_HOSTS = [
    ("web-01", "11", HostStatus.up, "Web Servers"),
    ("web-02", "12", HostStatus.up, "Web Servers"),
    ("db-01", "21", HostStatus.up, "Databases"),
    ("db-02", "22", HostStatus.down, "Databases"),
    ("cache-01", "31", HostStatus.up, "Cache"),
    ("app-01", "51", HostStatus.up, "App Servers"),
    ("app-02", "52", HostStatus.unknown, "App Servers"),
    ("mq-01", "61", HostStatus.up, "Messaging"),
    ("legacy-01", "71", HostStatus.disabled, "Decommissioned"),
]

_DYNATRACE_HOSTS = [
    ("frontend-01", "11", HostStatus.up, "frontend"),
    ("payments-01", "21", HostStatus.up, "payments"),
    ("payments-02", "22", HostStatus.down, "payments"),
    ("orders-01", "31", HostStatus.up, "orders"),
    ("search-01", "41", HostStatus.up, "search"),
    ("auth-01", "61", HostStatus.up, "auth"),
    ("gateway-01", "71", HostStatus.unknown, "gateway"),
]

_NNMI_HOSTS = [
    ("core-router-01", "1", HostStatus.up, "Core Network"),
    ("core-router-02", "2", HostStatus.up, "Core Network"),
    ("dist-switch-01", "11", HostStatus.up, "Distribution"),
    ("dist-switch-02", "12", HostStatus.down, "Distribution"),
    ("firewall-01", "21", HostStatus.up, "Security"),
    ("wan-router-01", "31", HostStatus.up, "WAN"),
    ("wan-router-02", "32", HostStatus.unknown, "WAN"),
    ("old-switch-09", "99", HostStatus.disabled, "Decommissioned"),
]


# --- Capacity trend profiles (for MOCK_MODE forecasting) --------------------
#
# Each mock host is assigned one of these by position, so every instance shows
# the full range of forecast outcomes rather than five variations of "ok".
# ``end_pct`` is where the drive sits TODAY, which is also what the Capacity
# page renders — the synthetic history is generated backwards from it, so the
# table and the forecast never contradict each other.

#: Days of synthetic history generated per mock host.
MOCK_HISTORY_DAYS = 35

#: label, mount, today's %, %/day, noise amplitude, GB, (resize day, GB before)
_TREND_PROFILES: list[dict] = [
    # Clearly filling: ~0.8 %/day lands it past 90% inside a fortnight.
    {"kind": "filling", "subject": "/var", "end_pct": 83.0, "slope": 0.8,
     "noise": 0.4, "total_gb": 500.0, "resize": None},
    # Flat. The line fits perfectly, so it reads as ok, not as noise.
    {"kind": "stable", "subject": "/data", "end_pct": 62.0, "slope": 0.0,
     "noise": 0.25, "total_gb": 1000.0, "resize": None},
    # Draining — a cleanup job that is winning.
    {"kind": "shrinking", "subject": "/backup", "end_pct": 44.0, "slope": -0.3,
     "noise": 0.3, "total_gb": 2000.0, "resize": None},
    # Upward drift buried in churn. Sits high enough that it would cross 90%
    # well inside the 90-day window — which is what makes the low R² matter,
    # and what the "noisy" classification exists to say.
    {"kind": "noisy", "subject": "/tmp", "end_pct": 72.0, "slope": 0.35,
     "noise": 14.0, "total_gb": 250.0, "resize": None},
    # Extended from 100 GB to 400 GB on day 20. Fitting across that cliff
    # would report a steep *fall*; fitting after it shows the real climb. The
    # post-resize rate is chosen to land in the "watch" band, so the estate
    # demonstrates every classification.
    {"kind": "resized", "subject": "/opt", "end_pct": 40.0, "slope": 0.8,
     "noise": 0.4, "total_gb": 400.0,
     "resize": {"day": 20, "total_gb": 100.0, "end_pct": 88.0, "slope": 0.9}},
]


def _jitter(seed_text: str, day: int, amplitude: float) -> float:
    """Deterministic pseudo-noise in ``[-amplitude, +amplitude]``.

    Digest-derived rather than :func:`hash`, which Python salts per process for
    strings — the same mock host would otherwise draw a different series on
    every restart, and a demo that changes shape between runs is not a demo.
    """
    if not amplitude:
        return 0.0
    digest = hashlib.blake2b(
        f"{seed_text}:{day}:capacity".encode(), digest_size=4
    ).digest()
    return (int.from_bytes(digest, "big") / 0xFFFFFFFF * 2.0 - 1.0) * amplitude


def trend_profile(name: str, idx: int, status: HostStatus) -> dict | None:
    """The capacity trend profile for one mock host, or None if it reports none."""
    if status == HostStatus.disabled:
        return None
    return _TREND_PROFILES[idx % len(_TREND_PROFILES)]


def profile_point(profile: dict, day_offset: int) -> tuple[float, float]:
    """``(used_pct, total_gb)`` for a profile ``day_offset`` days before today.

    ``day_offset`` counts backwards: 0 is today, 34 is the oldest sample.
    """
    resize = profile.get("resize")
    days_back = day_offset
    if resize and (MOCK_HISTORY_DAYS - 1 - days_back) < resize["day"]:
        # Before the resize: a smaller, nearly-full volume.
        day_index = MOCK_HISTORY_DAYS - 1 - days_back
        pct = resize["end_pct"] - (resize["day"] - 1 - day_index) * resize["slope"]
        return pct, resize["total_gb"]
    pct = profile["end_pct"] - days_back * profile["slope"]
    return pct, profile["total_gb"]


def _metrics(instance: str, idx: int, name: str, status: HostStatus) -> dict:
    """Deterministic, realistic capacity metrics for one mock host.

    Disabled hosts report no metrics (they're intentionally off); every other
    host gets a spread of CPU/memory/disk utilization plus sizing extras, so the
    Capacity view has hot and cold servers to look at.

    The per-mount ``filesystems`` entry carries the host's trend profile at its
    present-day value, so the drive the Capacity page shows is the same drive
    /forecast has a line for.
    """
    if status == HostStatus.disabled:
        return {"cpu_pct": None, "mem_pct": None, "disk_pct": None, "metrics": {}}
    seed = sum(ord(c) for c in f"{instance}:{name}")
    cpu = round(8 + (seed * 7 + idx * 13) % 86, 1)
    mem = round(20 + (seed * 5 + idx * 17) % 75, 1)
    disk = round(28 + (seed * 3 + idx * 11) % 66, 1)
    cores = [4, 8, 16, 32][seed % 4]
    mem_total = [8, 16, 32, 64, 128][seed % 5]
    disk_total = [120, 250, 500, 1000, 2000][seed % 5]
    metrics = {
        "cores": cores,
        "cpu_used_cores": round(cores * cpu / 100, 1),
        "mem_total_gb": mem_total,
        "mem_used_gb": round(mem_total * mem / 100, 1),
        "disk_total_gb": disk_total,
        "disk_used_gb": round(disk_total * disk / 100, 1),
    }
    profile = trend_profile(name, idx, status)
    if profile:
        pct, total_gb = profile_point(profile, 0)
        metrics["filesystems"] = [
            {
                "subject": profile["subject"],
                "used_gb": round(total_gb * pct / 100, 2),
                "total_gb": total_gb,
            }
        ]
    return {
        "cpu_pct": cpu,
        "mem_pct": mem,
        "disk_pct": disk,
        "metrics": metrics,
    }


def _hosts(instance: str, octet: int, rows: list[tuple]) -> list[dict]:
    slug = _slug(instance)
    out: list[dict] = []
    for idx, (name, tail, status, group) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{slug}-h{idx}",
                "hostname": f"{name}.{slug}",
                "ip": f"10.{octet}.{idx}.{tail}",
                "status": status,
                "group_name": group,
                "last_seen": _ago(idx),
                **_metrics(instance, idx, name, status),
                "raw_payload": {"mock": True, "instance": instance},
            }
        )
    return out


def mock_zabbix_hosts(instance: str) -> list[dict]:
    return _hosts(instance, 20, _ZABBIX_HOSTS)


def mock_dynatrace_hosts(instance: str) -> list[dict]:
    return _hosts(instance, 30, _DYNATRACE_HOSTS)


def mock_nnmi_hosts(instance: str) -> list[dict]:
    return _hosts(instance, 40, _NNMI_HOSTS)


# --- Alert templates (severity, native label, title, host suffix, mins ago) -
# The native label is the source tool's OWN severity wording (Zabbix has
# Disaster/High/Average; NNMi has Critical/Major/Minor; Dynatrace has
# Availability/Performance/…), shown as-is in the UI.

_ZABBIX_ALERTS = [
    (5, "Disaster", "Database replication stopped", "db-02", 8),
    (4, "High", "High CPU load (>90%)", "app-01", 22),
    (3, "Average", "Disk space 82% used", "cache-01", 47),
    (2, "Warning", "SSL certificate expires in 21 days", "web-01", 130),
]

_DYNATRACE_ALERTS = [
    (4, "Availability", "Service unavailable: payments-api", "payments-02", 5),
    (4, "Performance", "Response time degradation on orders-service", "orders-01", 18),
    (3, "Monitoring unavailable", "Monitoring unavailable", "gateway-01", 33),
    (3, "Resource", "Memory saturation", "search-01", 60),
]

_NNMI_ALERTS = [
    (5, "Critical", "Node down", "dist-switch-02", 12),
    (4, "Major", "Interface GigabitEthernet0/1 down", "core-router-01", 27),
    (3, "Minor", "High interface utilization", "wan-router-01", 55),
    (2, "Warning", "SNMP agent not responding", "wan-router-02", 90),
]


def _alerts(instance: str, rows: list[tuple]) -> list[dict]:
    slug = _slug(instance)
    out: list[dict] = []
    for idx, (sev, label, title, host, mins_ago) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{slug}-a{idx}",
                "host_hostname": f"{host}.{slug}",
                "severity_int": sev,
                "severity_label": label,
                "title": f"{title} on {host}.{slug}",
                "started_at": _ago(mins_ago),
                "raw_payload": {"mock": True, "instance": instance},
            }
        )
    return out


def mock_zabbix_alerts(instance: str) -> list[dict]:
    return _alerts(instance, _ZABBIX_ALERTS)


def mock_dynatrace_alerts(instance: str) -> list[dict]:
    return _alerts(instance, _DYNATRACE_ALERTS)


def mock_nnmi_alerts(instance: str) -> list[dict]:
    return _alerts(instance, _NNMI_ALERTS)

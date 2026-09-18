"""Deterministic problem-type normalization + fingerprinting — Correlation
Phase 2.

Two pure, DB-free functions. No AI/ML: every rule below is a fixed keyword
pattern, checked in order, first match wins.

:func:`normalize_problem_type` strips dynamic values out of a source's own
problem description so three alerts that only differ by a number —

    "CPU utilization is 91%"
    "CPU utilization is 94%"
    "CPU utilization is 97%"

— normalize to the same stable category (``CPU_HIGH``), while genuinely
different problems never collide: text that matches none of the known
categories falls back to a normalized (but NOT collapsed-to-one-bucket)
version of its own title, so "Queue backlog on X" and "Certificate expiring
on Y" — neither of which matches a known category — still end up as two
different fingerprints, not one catch-all "OTHER".

:func:`compute_fingerprint` is the actual dedup key: the resolved entity
plus the normalized problem type, plus whichever of metric/api/service/
database/network_device the event actually carries. Only present, relevant
fields go in — this is what keeps two different conditions on the same host
(CPU_HIGH and DISK_HIGH) from ever sharing a fingerprint, and what keeps two
different hosts with the same problem type from sharing one either (the
entity component differs).
"""

from __future__ import annotations

import re

#: (pattern, normalized label) — checked in order, first match wins. More
#: specific patterns are listed before more generic ones that could
#: otherwise swallow them (e.g. "monitoring unavailable" before the generic
#: "unavailable").
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcpu\b"), "CPU_HIGH"),
    (re.compile(r"\bmem(?:ory)?\b|\bswap\b"), "MEMORY_HIGH"),
    (re.compile(r"\bdisk\b|\bfilesystem\b|\bvolume\b|\bfree space\b|\bstorage\b"), "DISK_HIGH"),
    (re.compile(r"\bqueue\b"), "QUEUE_BACKLOG"),
    (re.compile(r"\bcertificate\b|\bssl\b|\btls\b|\bexpir"), "CERTIFICATE_ISSUE"),
    (re.compile(r"\bresource[_ ]contention\b"), "RESOURCE_CONTENTION"),
    (re.compile(r"\bmonitoring[_ ]unavailable\b"), "MONITORING_UNAVAILABLE"),
    (re.compile(r"\bresponse time\b|\blatency\b|\btimeout\b|\bslow\b|\bperformance\b"), "RESPONSE_SLOW"),
    (re.compile(r"\bdown\b|\bunavailable\b|\bnot available\b|\bunreachable\b|\boffline\b"), "AVAILABILITY_DOWN"),
    (re.compile(r"\berror\b|\bfail(?:ed|ure)?\b"), "ERROR"),
]

#: Numbers (with an optional leading sign/decimal and trailing %) and the
#: unit words that usually accompany them — the "dynamic values" the task
#: warns against fingerprinting on.
_DYNAMIC_VALUE_RE = re.compile(r"[-+]?\d+(?:\.\d+)?%?")
_UNIT_WORDS_RE = re.compile(
    r"\b(?:ms|sec|secs|seconds|minutes?|mins?|hours?|hrs?|mb|gb|kb|tb|bytes?|percent|pct)\b",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")

#: Column width on Alert.normalized_problem_type / LogicalEvent.normalized_problem_type.
_MAX_LEN = 64


def _fallback_normalize(text: str) -> str:
    """A deterministic, dynamic-value-free label for text no rule matched.

    Distinct inputs stay distinct (this is NOT a catch-all bucket) — only the
    numbers/units inside each one are stripped, same as the rule-matched path.
    """
    if not text:
        return "UNKNOWN"
    stripped = _DYNAMIC_VALUE_RE.sub(" ", text)
    stripped = _UNIT_WORDS_RE.sub(" ", stripped)
    stripped = stripped.upper()
    stripped = _NON_ALNUM_RE.sub("_", stripped).strip("_")
    return (stripped or "UNKNOWN")[:_MAX_LEN]


def normalize_problem_type(
    *, problem_type: str | None = None, title: str = "", metric_name: str | None = None,
) -> str:
    """Reduce a source's own problem description to a stable category.

    Checks ``problem_type`` (a real category where the source has one, e.g.
    Dynatrace's rankedEvents eventType), ``title`` (usually the most specific
    text available) and ``metric_name`` together, so a generic problem_type
    like Dynatrace's "CUSTOM_ALERT" still resolves correctly when the title
    says "CPU saturation on host X".
    """
    combined = " ".join(filter(None, [problem_type, title, metric_name])).lower()
    for pattern, label in _RULES:
        if pattern.search(combined):
            return label
    return _fallback_normalize(title or problem_type or metric_name or "")


def compute_fingerprint(
    *,
    entity_id: int | None,
    normalized_problem_type: str,
    metric_name: str | None = None,
    api_id: int | None = None,
    service_id: int | None = None,
    database_id: int | None = None,
    network_device_id: int | None = None,
) -> str | None:
    """The deterministic dedup key, or ``None`` if there is nothing to key on.

    ``None`` when ``entity_id`` is absent: without a resolved entity there is
    no reliable anchor to group on, and guessing one (e.g. from a raw
    hostname string) would risk exactly the "same host -> same fingerprint
    regardless of problem" merge the task explicitly forbids — better to
    leave the alert unlinked than to fingerprint it on weak ground.

    Kept as a readable composite string (not a hash) — with a fingerprint
    like ``entity:42|problem:CPU_HIGH`` you can read straight off it why two
    alerts did or didn't dedupe together, which is the whole point of
    "deterministic and explainable".
    """
    if entity_id is None:
        return None
    parts = [f"entity:{entity_id}", f"problem:{normalized_problem_type}"]
    if metric_name:
        parts.append(f"metric:{metric_name.strip().lower()}")
    # Only include a component when the event actually carries it — this is
    # what "use only relevant fields" means in practice: an alert with no
    # api_id never gets an "api:None" segment that would otherwise make every
    # api-less alert on an entity collide on that account alone.
    for label, value in (
        ("api", api_id), ("service", service_id),
        ("database", database_id), ("device", network_device_id),
    ):
        if value is not None:
            parts.append(f"{label}:{value}")
    return "|".join(parts)

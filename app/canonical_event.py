"""Per-source adapters: a collector's own normalized dict -> canonical fields.

Each collector's ``collect_alerts()``/``collect_resolved_alerts()`` already
turns its platform's native shape (a Zabbix trigger, a Dynatrace problem, an
NNMi incident, a SiteScope event) into the shared dict
:mod:`app.normalizer` upserts onto :class:`~app.models.Alert` — external_id,
host_hostname, severity_int/label, title, started_at, raw_payload. That part
is NOT duplicated here.

What lives here is strictly the extra mapping each source needs for the
*canonical* fields that dict does not already carry — one function per
source, so a source's own parsing quirks never leak into
:mod:`app.entity_resolution` or a later correlation phase:

    Zabbix trigger dict   -> adapt_zabbix_event()    -\\
    Dynatrace problem dict -> adapt_dynatrace_event() -+-> canonical fields
    NNMi incident dict    -> adapt_nnmi_event()       -|   merged onto Alert
    SiteScope event dict  -> adapt_sitescope_event()  -/   by app.normalizer

Everything returned here is read directly off the item's own already-parsed
fields and ``raw_payload`` — never re-fetched from the source. Fields no
current source exposes (metric_name/value/unit/threshold, application/
service/api/database/network_device identity, trace/span ids, environment/
location/business_service) are deliberately left for the caller to leave
NULL rather than guessed at here: inventing a value would be less
"explainable" than an honest gap a later phase can fill in once something
actually produces that data (e.g. once a collector reads Dynatrace's
Metrics v2 or RUM/trace APIs).
"""

from __future__ import annotations

from app.models import SourcePlatform

#: A fixed label per platform — the closest thing to "what kind of row is
#: this" that is knowable without inspecting the payload.
_EVENT_TYPE: dict[SourcePlatform, str] = {
    SourcePlatform.zabbix: "zabbix_trigger",
    SourcePlatform.dynatrace: "dynatrace_problem",
    SourcePlatform.nnmi: "nnmi_incident",
    SourcePlatform.sitescope: "sitescope_alert",
}


def _extract_tags(raw: dict) -> list[str]:
    """Best-effort tag/label passthrough from a raw payload.

    Checks the handful of key names these platforms actually use for tags
    (Zabbix trigger/host tags, Dynatrace entity tags). Returns ``[]`` rather
    than raising when a payload has none — most do not, today.
    """
    for key in ("tags", "entityTags", "labels"):
        value = raw.get(key)
        if not value:
            continue
        out: list[str] = []
        for t in value:
            if isinstance(t, str):
                out.append(t)
            elif isinstance(t, dict):
                k = t.get("key") or t.get("tag")
                v = t.get("value")
                if k and v:
                    out.append(f"{k}={v}")
                elif k or v:
                    out.append(str(k or v))
        if out:
            return out
    return []


def adapt_zabbix_event(item: dict) -> dict:
    """Zabbix's ``trigger.get``/``event.get`` dicts carry no distinct problem
    category beyond severity + a free-text description — ``problem_type``
    stays unset.
    """
    raw = item.get("raw_payload") or {}
    return {
        "event_type": _EVENT_TYPE[SourcePlatform.zabbix],
        "problem_type": None,
        "tags": _extract_tags(raw),
    }


def adapt_dynatrace_event(item: dict) -> dict:
    """Dynatrace Problems v2 exposes a real category via
    ``rankedEvents[0].eventType`` (e.g. ``AVAILABILITY_EVENT``) when present.
    """
    raw = item.get("raw_payload") or {}
    ranked = raw.get("rankedEvents") or []
    problem_type = None
    if ranked and isinstance(ranked[0], dict):
        problem_type = ranked[0].get("eventType")
    return {
        "event_type": _EVENT_TYPE[SourcePlatform.dynatrace],
        "problem_type": problem_type,
        "tags": _extract_tags(raw),
    }


def adapt_nnmi_event(item: dict) -> dict:
    """NNMi's IncidentBean rows (id/name/message/severity/source*) carry no
    distinct category field either — ``problem_type`` stays unset.
    """
    raw = item.get("raw_payload") or {}
    return {
        "event_type": _EVENT_TYPE[SourcePlatform.nnmi],
        "problem_type": None,
        "tags": _extract_tags(raw),
    }


def adapt_sitescope_event(item: dict) -> dict:
    """SiteScope has no separate category field either, but ``monitor_name``
    (the full group/monitor path already parsed by app/sitescope.py) is a
    reasonable, already-available stand-in for "what kind of check fired".
    """
    raw = item.get("raw_payload") or {}
    return {
        "event_type": _EVENT_TYPE[SourcePlatform.sitescope],
        "problem_type": item.get("monitor_name"),
        "tags": _extract_tags(raw),
    }


_ADAPTERS = {
    SourcePlatform.zabbix: adapt_zabbix_event,
    SourcePlatform.dynatrace: adapt_dynatrace_event,
    SourcePlatform.nnmi: adapt_nnmi_event,
    SourcePlatform.sitescope: adapt_sitescope_event,
}


def adapt_event(platform: SourcePlatform, item: dict) -> dict:
    """Dispatch to the adapter for ``platform``. Unknown platforms get ``{}``
    (e.g. Digital View, which is inventory-only and never produces alerts).
    """
    adapter = _ADAPTERS.get(platform)
    return adapter(item) if adapter else {}

"""Deterministic entity resolution — Correlation Phase 1.

Maps a source's own identifier for a thing (a Zabbix hostid, a Dynatrace
entityId, an NNMi node id) onto one shared :class:`~app.models.CanonicalEntity`,
so "Zabbix APP01", "Dynatrace app-prod-01" and "NNMi APP01_TE" all resolve to
the same identity when the evidence supports it.

No AI/ML, no fuzzy matching, no heuristic scoring. Every resolution is one of
a fixed, ordered list of exact-match methods
(:class:`~app.models.ResolutionMethod`), checked strongest-evidence-first:

1. ``manual_mapping``    — an admin explicitly declared this source id is this entity
2. ``cmdb_exact_match``  — a CMDB/asset id matches exactly
3. ``ip_exact_match``    — an IP address matches exactly
4. ``fqdn_exact_match``  — a fully-qualified domain name matches exactly
5. ``hostname_exact_match`` — a bare hostname matches exactly
6. ``alias_match``       — a registered alias matches exactly
7. ``source_mapping``    — this exact (platform, instance, external_id) was
   already resolved on a previous poll; keep that answer

If nothing matches, a new entity is created (``new_entity``) — this is not a
guess, it asserts nothing beyond "first time seeing this". If more than one
existing entity is an equally exact match (e.g. two different entities both
already claim the same IP — a real data-quality condition, not a hypothetical
one), resolution refuses to pick one: it returns ``conflict`` and leaves the
caller's entity_id unresolved rather than silently merging two identities on
a coin flip.

Every result records *how* it was reached (``method``) and a fixed confidence
per method, so a later phase — or a human looking at ``/entity-mappings`` —
can tell a manual override from a hostname guess.

Hostname matching is never trusted above stronger signals: it is checked
only after CMDB, IP and FQDN have all had a chance, precisely because two
unrelated hosts sharing a bare hostname (different environments, different
sites) is common enough that the priority order itself is the safeguard, not
an extra rule bolted on top of it.

Batched by design: :func:`resolve_hosts_batch` (used by
``app.normalizer.upsert_hosts`` and the SiteScope ingest path) prefetches
every candidate IP/hostname/FQDN/CMDB-id/manual-mapping for a WHOLE batch in
a handful of queries, then resolves each host from that in-memory context —
not one round trip per host. ``resolve_host_entity`` is the same algorithm
for a single host (tests, the manual mapping API, low-volume call sites) and
internally builds a one-item context, so there is exactly one matching
implementation either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CanonicalEntity,
    EntityAlias,
    EntityIP,
    EntityManualMapping,
    EntityType,
    ResolutionMethod,
    SourcePlatform,
)

#: Fixed confidence per method. Not a score derived from the data — a
#: constant that says how much a human should trust this *kind* of match.
CONFIDENCE: dict[ResolutionMethod, float] = {
    ResolutionMethod.manual_mapping: 1.0,
    ResolutionMethod.cmdb_exact_match: 0.99,
    ResolutionMethod.ip_exact_match: 0.95,
    ResolutionMethod.fqdn_exact_match: 0.9,
    ResolutionMethod.hostname_exact_match: 0.7,
    ResolutionMethod.alias_match: 0.6,
    ResolutionMethod.source_mapping: 0.5,
    ResolutionMethod.new_entity: 1.0,
    ResolutionMethod.conflict: 0.0,
}


@dataclass(frozen=True)
class ResolutionResult:
    """The outcome of one resolution attempt — always explainable."""

    entity_id: int | None
    method: ResolutionMethod
    confidence: float
    created_new: bool = False


_CONFLICT = ResolutionResult(entity_id=None, method=ResolutionMethod.conflict, confidence=0.0)


def _candidate_ips(ip: str | None, ip_all: str | None) -> list[str]:
    """Every distinct, non-empty IP a host reports, primary first."""
    out: list[str] = []
    if ip:
        out.append(ip.strip())
    if ip_all:
        for part in ip_all.split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    return value or None


@dataclass
class ResolutionContext:
    """Everything :func:`resolve_hosts_batch` needs, prefetched once.

    Populated from a handful of ``IN (...)`` queries covering a whole batch,
    then updated in memory as hosts resolve within it — a newly created
    entity's IP/alias is visible to the *next* item in the same batch
    without a further round trip (this is what lets three sources polled in
    one run still converge on one entity; between separate collector runs,
    each committed run makes the next one's prefetch see it from the DB as
    usual).
    """

    manual: dict[str, int] = field(default_factory=dict)
    cmdb: dict[str, int] = field(default_factory=dict)
    ip: dict[str, set[int]] = field(default_factory=dict)
    hostname: dict[str, set[int]] = field(default_factory=dict)
    fqdn: dict[str, set[int]] = field(default_factory=dict)
    alias: dict[str, set[int]] = field(default_factory=dict)


def build_resolution_context(
    db: Session, *, platform: SourcePlatform, instance: str, items: list[dict]
) -> ResolutionContext:
    """Prefetch every identifier a batch of host dicts might match on.

    ``items`` use the same keys as :func:`resolve_host_entity`'s parameters:
    ``external_id``, ``hostname``, ``ip``, ``ip_all``, ``fqdn``, ``cmdb_id``.
    """
    ips: set[str] = set()
    hostnames: set[str] = set()
    fqdns: set[str] = set()
    cmdb_ids: set[str] = set()
    identifiers: set[str] = set()

    for it in items:
        ips.update(_candidate_ips(it.get("ip"), it.get("ip_all")))
        h = _norm(it.get("hostname"))
        if h:
            hostnames.add(h)
        f = _norm(it.get("fqdn"))
        if f:
            fqdns.add(f)
        if it.get("cmdb_id"):
            cmdb_ids.add(it["cmdb_id"])
        if it.get("external_id"):
            identifiers.add(str(it["external_id"]))
        if it.get("hostname"):
            identifiers.add(str(it["hostname"]))

    ctx = ResolutionContext()

    if identifiers:
        for m in db.scalars(
            select(EntityManualMapping).where(
                EntityManualMapping.source_platform == platform,
                EntityManualMapping.source_instance == instance,
                EntityManualMapping.source_identifier.in_(identifiers),
            )
        ).all():
            ctx.manual[m.source_identifier] = m.entity_id

    if cmdb_ids:
        for cid, eid in db.execute(
            select(CanonicalEntity.cmdb_id, CanonicalEntity.id).where(
                CanonicalEntity.cmdb_id.in_(cmdb_ids)
            )
        ):
            ctx.cmdb[cid] = eid

    if ips:
        for ip_val, eid in db.execute(
            select(EntityIP.ip, EntityIP.entity_id).where(EntityIP.ip.in_(ips))
        ):
            ctx.ip.setdefault(ip_val, set()).add(eid)

    if hostnames:
        for alias, eid in db.execute(
            select(EntityAlias.alias, EntityAlias.entity_id).where(
                EntityAlias.alias_type == "hostname", EntityAlias.alias.in_(hostnames)
            )
        ):
            ctx.hostname.setdefault(alias, set()).add(eid)

    if fqdns:
        for alias, eid in db.execute(
            select(EntityAlias.alias, EntityAlias.entity_id).where(
                EntityAlias.alias_type == "fqdn", EntityAlias.alias.in_(fqdns)
            )
        ):
            ctx.fqdn.setdefault(alias, set()).add(eid)

    alias_candidates = hostnames | {_norm(i) for i in identifiers if _norm(i)}
    if alias_candidates:
        for alias, eid in db.execute(
            select(EntityAlias.alias, EntityAlias.entity_id).where(
                EntityAlias.alias_type == "alias", EntityAlias.alias.in_(alias_candidates)
            )
        ):
            ctx.alias.setdefault(alias, set()).add(eid)

    return ctx


def _register_ip(db: Session, ctx: ResolutionContext, entity_id: int, ip: str) -> None:
    """Claim ``ip`` for ``entity_id`` unless some entity already owns it.

    Trusts ``ctx`` (kept current as the batch resolves) instead of a
    check-before-insert SELECT — see :class:`ResolutionContext`. Never
    reassigns an address a different entity already owns.
    """
    owners = ctx.ip.get(ip)
    if owners:
        return  # already claimed, by this entity or another — leave it
    db.add(EntityIP(entity_id=entity_id, ip=ip))
    ctx.ip.setdefault(ip, set()).add(entity_id)


def _register_alias(
    db: Session, ctx: ResolutionContext, bucket: dict[str, set[int]],
    alias_type: str, entity_id: int, alias: str | None,
) -> None:
    norm = _norm(alias)
    if not norm or bucket.get(norm):
        return
    db.add(EntityAlias(entity_id=entity_id, alias_type=alias_type, alias=norm))
    bucket.setdefault(norm, set()).add(entity_id)


def _touch(
    db: Session,
    ctx: ResolutionContext,
    entity_id: int,
    method: ResolutionMethod,
    *,
    ips: list[str],
    hostname: str | None,
    fqdn: str | None,
    created_new: bool = False,
) -> ResolutionResult:
    """Record the match and opportunistically learn this source's identifiers.

    A hostname/IP seen via a *weaker* method than the one that will resolve
    it next time (e.g. Dynatrace's ``app-prod-01`` first resolved by IP)
    becomes a registered alias so ``alias_match``/a future prefetch can find
    it directly — without ever taking an address away from an entity that
    already owns it.
    """
    for ip in ips:
        _register_ip(db, ctx, entity_id, ip)
    _register_alias(db, ctx, ctx.hostname, "hostname", entity_id, hostname)
    _register_alias(db, ctx, ctx.fqdn, "fqdn", entity_id, fqdn)
    return ResolutionResult(entity_id, method, CONFIDENCE[method], created_new=created_new)


def create_manual_mapping(
    db: Session,
    *,
    entity_id: int,
    source_platform: SourcePlatform,
    source_instance: str,
    source_identifier: str,
    note: str | None = None,
) -> EntityManualMapping:
    """Declare ``source_identifier`` (on ``source_platform``/``source_instance``)
    as an explicit, admin-stated alias of ``entity_id``.

    This is the only resolution method that can apply *before* the source has
    ever reported the thing — every other method needs something to match
    against. Replaces any prior mapping for the same
    ``(platform, instance, source_identifier)`` rather than erroring, so
    correcting a mistaken mapping is just calling this again.
    """
    existing = db.scalar(
        select(EntityManualMapping).where(
            EntityManualMapping.source_platform == source_platform,
            EntityManualMapping.source_instance == source_instance,
            EntityManualMapping.source_identifier == source_identifier,
        )
    )
    if existing is not None:
        existing.entity_id = entity_id
        existing.note = note
        db.flush()
        return existing
    row = EntityManualMapping(
        entity_id=entity_id,
        source_platform=source_platform,
        source_instance=source_instance,
        source_identifier=source_identifier,
        note=note,
    )
    db.add(row)
    db.flush()
    return row


def _resolve(
    db: Session,
    ctx: ResolutionContext,
    *,
    platform: SourcePlatform,
    instance: str,
    external_id: str,
    hostname: str | None,
    ip: str | None,
    ip_all: str | None,
    fqdn: str | None,
    cmdb_id: str | None,
    prior_entity_id: int | None,
    entity_type: EntityType,
) -> ResolutionResult:
    ips = _candidate_ips(ip, ip_all)
    hostname_n = _norm(hostname)
    fqdn_n = _norm(fqdn)

    # 1. Explicit SAMI'X entity mapping.
    for candidate in (external_id, hostname):
        if candidate and candidate in ctx.manual:
            return _touch(
                db, ctx, ctx.manual[candidate], ResolutionMethod.manual_mapping,
                ips=ips, hostname=hostname, fqdn=fqdn,
            )

    # 2. CMDB id.
    if cmdb_id and cmdb_id in ctx.cmdb:
        return _touch(
            db, ctx, ctx.cmdb[cmdb_id], ResolutionMethod.cmdb_exact_match,
            ips=ips, hostname=hostname, fqdn=fqdn,
        )

    # 3. Exact IP.
    if ips:
        matches: set[int] = set()
        for ip_c in ips:
            matches |= ctx.ip.get(ip_c, set())
        if len(matches) == 1:
            return _touch(
                db, ctx, matches.pop(), ResolutionMethod.ip_exact_match,
                ips=ips, hostname=hostname, fqdn=fqdn,
            )
        if len(matches) > 1:
            return _CONFLICT

    # 4. Exact FQDN.
    if fqdn_n:
        matches = ctx.fqdn.get(fqdn_n, set())
        if len(matches) == 1:
            return _touch(
                db, ctx, next(iter(matches)), ResolutionMethod.fqdn_exact_match,
                ips=ips, hostname=hostname, fqdn=fqdn,
            )
        if len(matches) > 1:
            return _CONFLICT

    # 5. Exact hostname.
    if hostname_n:
        matches = ctx.hostname.get(hostname_n, set())
        if len(matches) == 1:
            return _touch(
                db, ctx, next(iter(matches)), ResolutionMethod.hostname_exact_match,
                ips=ips, hostname=hostname, fqdn=fqdn,
            )
        if len(matches) > 1:
            return _CONFLICT

    # 6. Alias (hostname or the source's own external id, registered under a
    # different platform/instance than the one currently resolving).
    alias_candidates = {v for v in (hostname_n, _norm(external_id)) if v}
    matches = set()
    for c in alias_candidates:
        matches |= ctx.alias.get(c, set())
    if len(matches) == 1:
        return _touch(
            db, ctx, next(iter(matches)), ResolutionMethod.alias_match,
            ips=ips, hostname=hostname, fqdn=fqdn,
        )
    if len(matches) > 1:
        return _CONFLICT

    # 7. Source-specific mapping: this exact source row was already resolved
    # on an earlier poll — keep that answer rather than re-deriving it.
    if prior_entity_id is not None:
        return _touch(
            db, ctx, prior_entity_id, ResolutionMethod.source_mapping,
            ips=ips, hostname=hostname, fqdn=fqdn,
        )

    # Nothing matched: this is the first time SAMI'X has seen this thing.
    entity = CanonicalEntity(
        entity_type=entity_type,
        canonical_name=hostname or external_id,
        cmdb_id=cmdb_id,
    )
    db.add(entity)
    db.flush()
    return _touch(
        db, ctx, entity.id, ResolutionMethod.new_entity,
        ips=ips, hostname=hostname, fqdn=fqdn, created_new=True,
    )


def resolve_host_entity(
    db: Session,
    *,
    platform: SourcePlatform,
    instance: str,
    external_id: str,
    hostname: str | None,
    ip: str | None = None,
    ip_all: str | None = None,
    fqdn: str | None = None,
    cmdb_id: str | None = None,
    prior_entity_id: int | None = None,
    entity_type: EntityType = EntityType.host,
) -> ResolutionResult:
    """Resolve one source row to a :class:`CanonicalEntity`, deterministically.

    ``prior_entity_id`` is this exact ``(platform, instance, external_id)``'s
    *previously* resolved entity, if any — pass the caller's already-loaded
    value (e.g. ``existing_row.entity_id``) rather than making this function
    query for it; it is only consulted last (``source_mapping``), so a host
    whose IP only becomes known on a later poll still gets the chance to
    resolve by IP first instead of being stuck on an earlier, weaker match.

    Resolving hosts one at a time like this costs a handful of queries per
    call — fine for a single event (SiteScope push, the manual mapping API,
    tests) but not for a full poll batch. :func:`resolve_hosts_batch` is the
    same algorithm prefetched once for many hosts.
    """
    ctx = build_resolution_context(
        db, platform=platform, instance=instance,
        items=[{
            "external_id": external_id, "hostname": hostname, "ip": ip,
            "ip_all": ip_all, "fqdn": fqdn, "cmdb_id": cmdb_id,
        }],
    )
    return _resolve(
        db, ctx, platform=platform, instance=instance, external_id=external_id,
        hostname=hostname, ip=ip, ip_all=ip_all, fqdn=fqdn, cmdb_id=cmdb_id,
        prior_entity_id=prior_entity_id, entity_type=entity_type,
    )


def resolve_named_entity(db: Session, *, entity_type: EntityType, name: str) -> int:
    """Resolve an application-layer entity (service/api/database/application/
    external_service/business_transaction/business_service — Correlation
    Phase 5) by exact name within its own type.

    Deliberately NOT :func:`resolve_host_entity`: that resolver's hostname/
    alias buckets are shared across every entity it has ever seen, with no
    entity_type filter at any step — safe when only host/service/
    network_device existed (topology_sync's own service and network_device
    names rarely collided with a hostname), but risky now that api/database/
    application names come from free-text trace data and could coincidentally
    match an unrelated entity of a different kind. A plain, type-scoped exact
    match on ``canonical_name`` has no such cross-type collision risk and
    needs no IP/FQDN evidence chain — a trace span names its own service/api/
    database directly, there is nothing weaker to fall back through.
    """
    name = name.strip()
    existing = db.scalar(
        select(CanonicalEntity).where(
            CanonicalEntity.entity_type == entity_type,
            CanonicalEntity.canonical_name == name,
        )
    )
    if existing is not None:
        return existing.id
    entity = CanonicalEntity(entity_type=entity_type, canonical_name=name)
    db.add(entity)
    db.flush()
    return entity.id


def resolve_hosts_batch(
    db: Session,
    *,
    platform: SourcePlatform,
    instance: str,
    items: list[dict],
) -> dict[str, ResolutionResult]:
    """Resolve a whole batch of hosts with a small, fixed number of queries.

    ``items`` use the same keys as :func:`resolve_host_entity`'s parameters,
    plus ``prior_entity_id``. Returns a dict keyed by each item's
    ``external_id``. Callers should only include items whose identity
    signals actually need (re-)resolving — see the "identity_unchanged"
    check in ``app.normalizer.upsert_hosts`` — so a steady-state poll with
    no new or changed hosts calls this with an empty list and pays nothing.
    """
    if not items:
        return {}
    ctx = build_resolution_context(db, platform=platform, instance=instance, items=items)
    out: dict[str, ResolutionResult] = {}
    for it in items:
        external_id = str(it["external_id"])
        out[external_id] = _resolve(
            db, ctx, platform=platform, instance=instance, external_id=external_id,
            hostname=it.get("hostname"), ip=it.get("ip"), ip_all=it.get("ip_all"),
            fqdn=it.get("fqdn"), cmdb_id=it.get("cmdb_id"),
            prior_entity_id=it.get("prior_entity_id"),
            entity_type=it.get("entity_type", EntityType.host),
        )
    return out

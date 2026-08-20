"""Host owner resolution — who to escalate an alert to.

Learned from the user's ``zabbix_owner_export.py`` and generalized so both
Zabbix and Dynatrace hosts resolve an owner the same way. An owner is a person
(or team) responsible for a server; ``owner_email`` is the address the Escalate
action drafts a mail to. Everything here is pure/best-effort — never raises.
"""

from __future__ import annotations

import re

#: Tag/field names that carry an owner, lowercased. A tag key that merely
#: *contains* one of these also matches (e.g. Dynatrace "Server Owner").
OWNER_KEYS = {
    "owner", "host owner", "host_owner", "host-owner",
    "server owner", "server_owner", "server-owner",
    "contact", "responsible", "responsible person",
    "owner_email", "owner email", "contact_email", "contact email",
    "email",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_email(value: str | None) -> str | None:
    """First email address found inside a free-text string, or None."""
    if not value:
        return None
    m = _EMAIL_RE.search(value)
    return m.group(0) if m else None


def _key_matches(key: str) -> bool:
    key = key.strip().lower()
    return key in OWNER_KEYS or any(k in key for k in ("owner", "responsible", "contact"))


def owner_from_tags(tags: list | None) -> tuple[str | None, str | None]:
    """(owner, owner_email) from a list of {tag/key, value} dicts.

    Accepts both Zabbix (``tag``/``value``) and Dynatrace (``key``/``value``)
    tag shapes. An email-bearing tag fills ``owner_email``; the first plain
    owner-ish tag fills ``owner``.
    """
    owner: str | None = None
    owner_email: str | None = None
    for t in tags or []:
        if not isinstance(t, dict):
            continue
        key = str(t.get("tag") or t.get("key") or "").strip()
        value = str(t.get("value") or "").strip()
        if not value or not _key_matches(key):
            continue
        email = extract_email(value)
        if email and owner_email is None:
            owner_email = email
        if owner is None:
            owner = value
    return owner, owner_email


#: Dynatrace owner tag keys, matched EXACTLY (case-insensitive) — from the
#: user's dynatrace_hosts_owners.py. Exact match (not substring) avoids false
#: positives like a "teamwork" tag matching "team".
_DT_OWNER_TAG_KEYS = {
    "owner", "ownership", "owned_by", "team",
    "department", "contact", "poc", "responsible",
}


def dynatrace_owner(entity: dict) -> tuple[str | None, str | None]:
    """(owner, owner_email) for a Dynatrace HOST entity.

    Ported from the user's export script's resolution order:

    1. a tag whose key exactly matches an owner key AND has a value;
    2. a tag encoded as ``owner:someone`` in ``stringRepresentation``;
    3. management zones (a proxy for ownership);
    4. the host group name (a weaker proxy).

    ``owner_email`` is filled only when an email is actually present (tags),
    never for the zone/host-group proxies.
    """
    tags = entity.get("tags") or []
    props = entity.get("properties") or {}

    # 1) exact-key tag with a value
    for t in tags:
        if not isinstance(t, dict):
            continue
        key = (t.get("key") or "").strip().lower()
        val = (t.get("value") or "").strip()
        if key in _DT_OWNER_TAG_KEYS and val:
            return val, extract_email(val)

    # 2) "owner:someone" packed into stringRepresentation
    for t in tags:
        if not isinstance(t, dict):
            continue
        key = (t.get("key") or "").strip().lower()
        if key in _DT_OWNER_TAG_KEYS:
            s = (t.get("stringRepresentation") or "").strip()
            if ":" in s:
                val = s.split(":", 1)[1].strip()
                if val:
                    return val, extract_email(val)

    # 3) management zones (proxy)
    zones = [
        m.get("name", "")
        for m in (entity.get("managementZones") or [])
        if isinstance(m, dict) and m.get("name")
    ]
    if zones:
        return ", ".join(zones), None

    # 4) host group (weaker proxy)
    hg = props.get("hostGroupName")
    if hg:
        return str(hg), None

    return None, None


def resolve_owner(
    tags: list | None = None,
    inventory: dict | None = None,
) -> tuple[str | None, str | None]:
    """Best (owner, owner_email) from tags first, then Zabbix inventory.

    Order mirrors the export script: owner tags win, then inventory ``contact``,
    then inventory ``alias``. ``owner_email`` is any email seen along the way
    (or extracted from the chosen owner string).
    """
    owner, owner_email = owner_from_tags(tags)

    inv = inventory or {}
    if owner is None:
        contact = str(inv.get("contact") or "").strip()
        if contact:
            owner = contact
        else:
            alias = str(inv.get("alias") or "").strip()
            if alias:
                owner = alias
    if owner_email is None:
        owner_email = (
            extract_email(str(inv.get("contact") or ""))
            or extract_email(owner)
        )
    return owner or None, owner_email or None

"""Per-host Zabbix capacity detail — ported from the weekly-report script.

Reuses an authenticated :class:`~app.collectors.zabbix.ZabbixCollector` (its
``_rpc`` handles login/session and credentials from ``servers.yaml``), so NO
credentials or session handling are duplicated here. For one host it pulls the
cpu/memory/disk items, their current values, and trend min/avg/max over a
window, and returns a structured dict for the Capacity detail panel.

Results are cached per (instance, hostid) for POLL_INTERVAL so repeated clicks
don't hammer the Zabbix API.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

from app.config import get_settings

GB = 1024.0 ** 3

#: Item keys pulled (server-side search keeps the response light).
_KEY_SEARCH = [
    "system.cpu.num", "system.cpu.util",
    "vm.memory.size[", "vm.memory.utilization", "vm.memory.util",
    "vfs.fs.size[", "vfs.fs.dependent.size[",
]


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def key_params(key: str) -> list[str]:
    """``vfs.fs.size[/var,total]`` -> ``['/var', 'total']`` (quote/bracket aware)."""
    m = re.match(r"^[^\[]+\[(.*)\]$", key.strip())
    if not m:
        return []
    raw, out, buf, depth, quoted = m.group(1), [], "", 0, False
    for ch in raw:
        if ch == '"' and depth == 0:
            quoted = not quoted
            buf += ch
        elif ch == "[" and not quoted:
            depth += 1
            buf += ch
        elif ch == "]" and not quoted:
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0 and not quoted:
            out.append(buf.strip().strip('"'))
            buf = ""
        else:
            buf += ch
    out.append(buf.strip().strip('"'))
    return out


def _fs_label(item: dict) -> str:
    name = (item.get("name") or "").strip()
    label = re.sub(
        r"\s*:\s*(total space|used space|space utilization|total|used|free space|pused).*$",
        "", name, flags=re.I,
    ).strip()
    m = re.match(r"^(?:FS|Filesystem|Mounted filesystem)\s*\[(.+)\]$", label, re.I)
    if m:
        label = m.group(1).strip()
    if not label:
        p = key_params(item["key_"])
        label = p[0] if p else item["key_"]
    return label


def _classify(items: list[dict]) -> dict:
    """One host's items -> the cpu/mem items and per-filesystem total/used items."""
    res = {"cpu_num": None, "cpu_util": None, "mem_total": None,
           "mem_util": None, "mem_util_inverted": False, "fs": {}}
    cpu_rank = mem_rank = 99
    for it in items:
        low = it["key_"].lower()
        name_low = (it.get("name") or "").lower()
        if low.startswith("system.cpu.num") and res["cpu_num"] is None:
            res["cpu_num"] = it
        rank = None
        if low == "system.cpu.util":
            rank = 0
        elif low.startswith("system.cpu.util[") and ",idle" not in low:
            rank = 1
        elif name_low in ("cpu utilization", "cpu utilisation", "cpu usage"):
            rank = 3
        if rank is not None and rank < cpu_rank:
            cpu_rank, res["cpu_util"] = rank, it
        if low == "vm.memory.size[total]":
            res["mem_total"] = it
        mrank, inv = None, False
        if low in ("vm.memory.utilization", "vm.memory.util"):
            mrank = 0
        elif low == "vm.memory.size[pused]":
            mrank = 1
        elif low == "vm.memory.size[pavailable]":
            mrank, inv = 2, True
        elif name_low in ("memory utilization", "memory used in %", "memory usage"):
            mrank = 3
        if mrank is not None and mrank < mem_rank:
            mem_rank = mrank
            res["mem_util"], res["mem_util_inverted"] = it, inv
        if low.startswith("vfs.fs.size[") or low.startswith("vfs.fs.dependent.size["):
            p = key_params(it["key_"])
            if len(p) < 2:
                continue
            fsname, mode = p[0], p[1].lower()
            if mode not in ("total", "used"):
                continue
            slot = res["fs"].setdefault(fsname, {"label": None, "total": None, "used": None})
            slot[mode] = it
            if slot["label"] is None or mode == "total":
                slot["label"] = _fs_label(it)
    return res


def _last_values(collector, items: list[dict]) -> dict:
    """itemid -> float, from item lastvalue with a history.get fallback."""
    last, missing = {}, []
    for it in items:
        v = _to_float(it.get("lastvalue"))
        (last.__setitem__(it["itemid"], v) if v is not None else missing.append(it))
    if not missing:
        return last
    by_type = defaultdict(list)
    for it in missing:
        by_type[int(it.get("value_type", 0))].append(it["itemid"])
    now = int(time.time())
    for vtype, ids in by_type.items():
        if vtype not in (0, 3):
            continue
        rows = collector._rpc("history.get", {
            "history": vtype, "itemids": ids, "time_from": now - 3 * 3600,
            "output": "extend", "sortfield": "clock", "sortorder": "DESC",
            "limit": len(ids) * 40,
        })
        for r in rows:  # newest first
            last.setdefault(r["itemid"], _to_float(r["value"]))
    return {k: v for k, v in last.items() if v is not None}


def _trends(collector, itemids: list[str], days: int) -> dict:
    """itemid -> (min, avg, max) over the window (avg weighted by sample count)."""
    if not itemids:
        return {}
    now = int(time.time())
    rows = collector._rpc("trend.get", {
        "itemids": itemids, "time_from": now - days * 86400, "time_till": now,
        "output": ["itemid", "num", "value_min", "value_avg", "value_max"],
    })
    agg = {}
    for r in rows:
        iid = r["itemid"]
        n = int(r.get("num") or 1)
        vmin, vavg, vmax = _to_float(r["value_min"]), _to_float(r["value_avg"]), _to_float(r["value_max"])
        if vavg is None:
            continue
        a = agg.setdefault(iid, {"min": vmin, "max": vmax, "sum": 0.0, "n": 0})
        if vmin is not None:
            a["min"] = vmin if a["min"] is None else min(a["min"], vmin)
        if vmax is not None:
            a["max"] = vmax if a["max"] is None else max(a["max"], vmax)
        a["sum"] += vavg * n
        a["n"] += n
    return {i: (a["min"], a["sum"] / a["n"] if a["n"] else None, a["max"]) for i, a in agg.items()}


# --- cache ------------------------------------------------------------------
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _cache_ttl() -> int:
    return max(30, get_settings().poll_interval_minutes * 60)


def host_capacity_detail(collector, hostid: str, days: int = 7) -> dict:
    """Return the capacity detail for one Zabbix host (cached per POLL_INTERVAL)."""
    key = (collector.instance, str(hostid))
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    host_rows = collector._rpc("host.get", {
        "hostids": [hostid], "output": ["hostid", "host", "name"],
        "selectInterfaces": ["ip", "main"],
    })
    hinfo = host_rows[0] if host_rows else {"name": str(hostid)}
    ip = "—"
    for want in ("1", "0"):
        for i in hinfo.get("interfaces", []) or []:
            if i.get("main") == want and i.get("ip"):
                ip = i["ip"]
                break
        if ip != "—":
            break

    items = collector._rpc("item.get", {
        "hostids": [hostid],
        "output": ["itemid", "hostid", "key_", "name", "value_type", "units", "lastvalue"],
        "search": {"key_": _KEY_SEARCH}, "searchByAny": True, "startSearch": True,
        "filter": {"status": 0}, "webitems": False,
    })
    c = _classify(items)
    last = _last_values(collector, items)
    trend_ids = [c[k]["itemid"] for k in ("cpu_util", "mem_util") if c[k]]
    stats = _trends(collector, trend_ids, days)

    def lv(it):
        return last.get(it["itemid"]) if it else None

    cores = lv(c["cpu_num"])
    cpu_now = lv(c["cpu_util"])
    mem_tot = lv(c["mem_total"])
    mem_now = lv(c["mem_util"])
    if mem_now is not None and c["mem_util_inverted"]:
        mem_now = 100.0 - mem_now

    def trend(it, inv=False):
        s = stats.get(it["itemid"]) if it else None
        if not s:
            return None
        if inv:
            s = (100 - s[2], 100 - s[1], 100 - s[0])
        return {"min": s[0], "avg": s[1], "max": s[2]}

    filesystems, tot_sum, used_sum = [], 0.0, 0.0
    for fsname, slot in sorted(c["fs"].items()):
        t = lv(slot["total"])
        u = lv(slot["used"])
        if t:
            tot_sum += t
        if u:
            used_sum += u
        filesystems.append({
            "label": slot["label"] or fsname,
            "total_gb": round(t / GB, 1) if t else None,
            "used_gb": round(u / GB, 1) if u else None,
            "pct": round(u / t * 100, 1) if (t and u) else None,
        })

    detail = {
        "host": hinfo.get("name") or hinfo.get("host") or str(hostid),
        "ip": ip,
        "cores": int(cores) if cores is not None else None,
        "cpu_pct": round(cpu_now, 1) if cpu_now is not None else None,
        "mem_total_gb": round(mem_tot / GB, 1) if mem_tot else None,
        "mem_pct": round(mem_now, 1) if mem_now is not None else None,
        "disk_total_gb": round(tot_sum / GB, 1) if tot_sum else None,
        "disk_used_gb": round(used_sum / GB, 1) if used_sum else None,
        "disk_pct": round(used_sum / tot_sum * 100, 1) if tot_sum else None,
        "cpu_trend": trend(c["cpu_util"]),
        "mem_trend": trend(c["mem_util"], c["mem_util_inverted"]),
        "filesystems": filesystems,
        "days": days,
    }
    _cache[key] = (time.time() + _cache_ttl(), detail)
    return detail

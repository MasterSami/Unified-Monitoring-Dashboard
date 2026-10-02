"""Role 4 - the evidence packager and the output validator.

The pack is the only thing the model is allowed to answer from. Every row a
tool returned becomes a FACT with its source platform, instance and the time
that platform was last synced. Anything SAMIX could not get - a failed tool,
a platform whose last collector run failed - becomes an UNKNOWN, worded so
the model can repeat it instead of papering over it.

The validator is deliberately dumb and deterministic: hostnames and numbers
in the answer that look like data must exist somewhere in the pack. When it
fails, the caller shows the pack itself, so the user is never left without
the data because the prose was wrong.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import PLATFORM_ORDER, RunStatus
from app.scheduler import _latest_runs_by_instance
from app.sitescope import dedup_key

from .tools import ToolResult

_PLATFORM_LABEL = {
    "zabbix": "Zabbix", "dynatrace": "Dynatrace", "nnmi": "NNMi",
    "sitescope": "SiteScope", "digitalview": "Digital View",
}


def platform_label(name: str | None) -> str:
    return _PLATFORM_LABEL.get((name or "").lower(), name or "-")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


# --- Freshness ----------------------------------------------------------------


def freshness_by_instance(db: Session) -> dict[str, dict[str, Any]]:
    """``{instance: {platform, status, last_run_at, last_success_at, error}}``
    from the collector run log - two queries, same as the sidebar."""
    latest = _latest_runs_by_instance(db)
    success = _latest_runs_by_instance(db, only_success=True)
    out: dict[str, dict[str, Any]] = {}
    for instance, run in latest.items():
        ok = success.get(instance)
        out[instance] = {
            "platform": run.platform,
            "status": run.status.value,
            "last_run_at": _iso(run.started_at),
            "last_success_at": _iso(ok.finished_at or ok.started_at) if ok else None,
            "error": (run.error_message or "")[:200] if run.status == RunStatus.failed else None,
        }
    return out


def freshness_line(freshness: dict[str, dict[str, Any]]) -> str:
    """'Data as of: Zabbix 12:05, Dynatrace 12:03, NNMi no sync yet'."""
    best: dict[str, str | None] = {}
    for info in freshness.values():
        p = info["platform"]
        t = info.get("last_success_at")
        if p not in best or (t and (best[p] is None or t > best[p])):
            best[p] = t
    parts = []
    for p in sorted(best, key=lambda x: PLATFORM_ORDER.index(x) if x in PLATFORM_ORDER else 99):
        t = best[p]
        parts.append(f"{platform_label(p)} {t[11:16]}" if t else f"{platform_label(p)} no sync yet")
    return "Data as of: " + (", ".join(parts) if parts else "no collector has run yet")


# --- Pack -----------------------------------------------------------------------


@dataclass
class EvidencePack:
    question: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    freshness: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per tool: what was asked and what came back (resolution, counts).
    tool_notes: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def is_empty(self) -> bool:
        return not self.facts

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "facts": self.facts,
            "unknowns": self.unknowns,
            "freshness": self.freshness,
            "tool_notes": self.tool_notes,
        }

    def prompt_json(self) -> str:
        """Compact JSON for the answer prompt (no indentation: tokens matter)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)


def build_pack(question: str, results: list[ToolResult], freshness: dict[str, dict[str, Any]]) -> EvidencePack:
    pack = EvidencePack(question=question, freshness=freshness)
    seen_ids: set[str] = set()
    platforms_seen: set[str] = set()

    for r in results:
        note: dict[str, Any] = {"tool": r.name, "args": r.args, "ok": r.ok,
                                "rows": len(r.rows), "truncated": r.truncated}
        if r.meta:
            note["meta"] = {k: v for k, v in r.meta.items() if k != "message"}
            if r.meta.get("message"):
                note["result"] = r.meta["message"]
        pack.tool_notes.append(note)
        if not r.ok:
            pack.unknowns.append(f"{r.name} could not run: {r.error}. Nothing from it is known.")
            continue
        if r.truncated:
            pack.unknowns.append(
                f"{r.name} returned {r.total_rows} rows; only the first {len(r.rows)} are included."
            )
        for row in r.rows:
            rid = str(row.get("record_id") or f"{r.name}:{len(seen_ids)}")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            platform = row.get("source_platform")
            instance = row.get("source_instance") or ""
            platforms_seen.add(platform or "")
            sync = freshness.get(instance, {})
            pack.facts.append({
                "id": rid,
                "type": "FACT",
                "tool": r.name,
                "source_platform": platform,
                "source_instance": instance,
                "as_of": sync.get("last_success_at") or row.get("last_seen") or row.get("computed_at") or row.get("updated_at"),
                "data": {k: v for k, v in row.items() if k not in ("record_id", "source_platform", "source_instance")},
            })

    # A platform whose last run failed has silently contributed nothing, which
    # a fact list cannot show. Say it, with the last time it did work.
    for instance, info in sorted(freshness.items()):
        if info["status"] == RunStatus.failed.value:
            last = info.get("last_success_at")
            pack.unknowns.append(
                f"{platform_label(info['platform'])} data from {instance} is unavailable "
                f"(last collector run failed; last successful sync: {last[11:16] if last else 'never'})."
            )
    if results and not freshness:
        pack.unknowns.append("No collector has run yet, so SAMIX holds no synced data.")
    return pack


# --- Output validation ----------------------------------------------------------

#: Tokens that read as a hostname or address: letters/digits joined by '-' or
#: '.' with at least one separator (web-01, mw-10, web-01.zabbix-dc1, 10.20.1.11).
_HOSTLIKE = re.compile(r"(?<![\w/])[a-z0-9]+(?:[-.][a-z0-9]+)+(?![\w/])", re.I)
_NUMBER = re.compile(r"(?<![\w.:-])\d+(?:\.\d+)?(?![\w.:-])")
_WORD_NUMBER_OK = {"0", "1"}

#: Phrases that count as "there is no data" in either language.
_NO_DATA_MARKERS = (
    "no match", "no alert", "no data", "no host", "not found", "nothing", "none", "no active",
    "unknown", "unavailable", "could not", "couldn't", "no record", "not monitored", "no problem",
    "لا ", "مش ", "مفيش", "مافيش", "ما فيش", "غير", "لم ", "مالوش", "ملوش", "ما ", "مش موجود", "مفيش بيانات",
)


@dataclass
class Validation:
    passed: bool
    problems: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "passed" if self.passed else "failed"


def _pack_tokens(pack: EvidencePack) -> tuple[set[str], set[str], set[str]]:
    """(host-like tokens, numbers, derived counts) present in the pack."""
    blob = json.dumps(pack.to_dict(), ensure_ascii=False, default=str).lower()
    hosts = {m.group(0) for m in _HOSTLIKE.finditer(blob)}
    hosts |= {dedup_key(h) for h in hosts}
    # Plain hostnames without separators (e.g. "mw10") still count as present.
    for f in pack.facts:
        for key in ("hostname", "host"):
            v = f["data"].get(key)
            if isinstance(v, str) and v:
                hosts.add(v.lower()); hosts.add(dedup_key(v))
    numbers: set[str] = set()
    for m in _NUMBER.finditer(blob):
        n = m.group(0)
        numbers.add(n)
        if "." in n:
            numbers.add(str(int(round(float(n)))))
            numbers.add(n.rstrip("0").rstrip("."))
    # Also every digit run inside timestamps/ids (12:05 -> 12, 05, 5).
    for m in re.finditer(r"\d+", blob):
        numbers.add(m.group(0)); numbers.add(m.group(0).lstrip("0") or "0")

    counts = {str(len(pack.facts)), str(len(pack.unknowns))}
    per: dict[str, int] = {}
    for f in pack.facts:
        for key in ("source_platform", "tool"):
            per[f"{key}={f.get(key)}"] = per.get(f"{key}={f.get(key)}", 0) + 1
        for key in ("severity", "severity_label", "classification", "state", "status", "resource"):
            v = f["data"].get(key)
            if v is not None:
                per[f"{key}={v}"] = per.get(f"{key}={v}", 0) + 1
    counts |= {str(v) for v in per.values()}
    for note in pack.tool_notes:
        counts.add(str(note.get("rows", 0)))
        for v in (note.get("meta") or {}).values():
            if isinstance(v, dict):
                counts |= {str(x) for x in v.values() if isinstance(x, int)}
            elif isinstance(v, int):
                counts.add(str(v))
    return hosts, numbers, counts


def validate_answer(answer: str, pack: EvidencePack) -> Validation:
    """Every hostname-looking token and every data-looking number in the
    answer must appear in the pack (or be a count derivable from it). An
    answer to an empty pack must say there is nothing."""
    problems: list[str] = []
    text = (answer or "").strip()
    low = text.lower()
    if not text:
        return Validation(False, ["empty answer"])

    if pack.is_empty:
        if not any(marker in low for marker in _NO_DATA_MARKERS):
            problems.append("the evidence pack is empty but the answer does not say so")

    hosts, numbers, counts = _pack_tokens(pack)
    question_tokens = {m.group(0).lower() for m in _HOSTLIKE.finditer(pack.question or "")}
    question_tokens |= {dedup_key(t) for t in question_tokens}

    for m in _HOSTLIKE.finditer(text):
        tok = m.group(0).lower()
        if re.fullmatch(r"[\d.]+", tok) and tok.count(".") < 3:
            continue  # a decimal like 83.0, handled as a number below
        if tok in hosts or dedup_key(tok) in hosts or tok in question_tokens:
            continue
        problems.append(f"hostname/address '{m.group(0)}' is not in the evidence")

    for m in _NUMBER.finditer(text):
        n = m.group(0)
        if n in _WORD_NUMBER_OK or n in numbers or n in counts or n.lstrip("0") in numbers:
            continue
        if "." in n and n.rstrip("0").rstrip(".") in numbers:
            continue
        problems.append(f"number '{n}' is not in the evidence")

    return Validation(not problems, problems)


# --- Rendering ------------------------------------------------------------------

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def render_answer_html(answer: str) -> str:
    """Escape the model's text and turn the little markdown it tends to emit
    (bold, '-' bullets, numbered lines) into HTML. Nothing else is trusted."""
    lines = html.escape(answer or "").replace("\r", "").split("\n")
    out: list[str] = []
    in_list: str | None = None

    def inline(text: str) -> str:
        return _BOLD.sub(r"<b>\1</b>", text)

    def close() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for raw in lines:
        line = raw.rstrip()
        bullet = re.match(r"^\s*[-*•]\s+(.*)$", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet or number:
            kind = "ul" if bullet else "ol"
            if in_list != kind:
                close()
                out.append(f"<{kind}>")
                in_list = kind
            item = inline((bullet or number).group(1))
            out.append(f"<li>{item}</li>")
            continue
        close()
        if line.strip():
            para = inline(line)
            out.append(f"<p>{para}</p>")
    close()
    return "\n".join(out)

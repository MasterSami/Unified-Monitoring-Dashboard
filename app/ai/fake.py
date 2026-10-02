"""A deterministic stand-in for the model, for tests and offline benchmarks.

It plays the same two-call game the real model does - pick a tool from the
question, then write an answer from the Evidence Pack - using keyword rules
instead of a neural net. It exists so the whole ask flow (gateway, broker,
evidence, validator, audit, UI) can be exercised with no Ollama present, and
so the benchmark has a floor to compare real models against.
"""

from __future__ import annotations

import json
import re
from typing import Any

_HOST_TOKEN = re.compile(r"(?<![\w/])[A-Za-z][A-Za-z0-9]*[-.]?[A-Za-z0-9]*\d[A-Za-z0-9.-]*(?![\w/])")
_CAPACITY = re.compile(r"capacity|disk|drive|memory|90\s*%|full|forecast|ديسك|مساحة|الهارد|كاباسيتي|هيخلص|يخلص", re.I)
_STATUS = re.compile(r"monitor|status|up\b|down\b|reachable|مراقب|حالت|شغال|واقع|متابع", re.I)
_WORST = re.compile(r"worst|top|critical|severe|now|happening|أخطر|اخطر|اعلى|دلوقتي|حاليا|حالياً|الاعلى", re.I)


class FakeLLM:
    model = "fake-deterministic"

    def __init__(self, *, force_tool: tuple[str, dict[str, Any]] | None = None,
                 answer_override: str | None = None) -> None:
        self.force_tool = force_tool
        self.answer_override = answer_override
        self.calls: list[dict[str, Any]] = []

    # --- protocol -------------------------------------------------------------

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> dict[str, Any]:
        self.calls.append({"messages": len(messages), "tools": bool(tools)})
        if tools:
            if any(m.get("role") == "tool" for m in messages):
                return {"role": "assistant", "content": "ready"}
            question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
            name, args = self.force_tool or self._pick(question)
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": args}}]}
        prompt = messages[-1]["content"]
        if self.answer_override is not None:
            return {"role": "assistant", "content": self.answer_override}
        return {"role": "assistant", "content": self._answer(prompt)}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "model_present": True, "models": [self.model], "error": None}

    # --- rules ----------------------------------------------------------------

    @staticmethod
    def _pick(question: str) -> tuple[str, dict[str, Any]]:
        q = question.split("\n", 1)[0]
        host = next((m.group(0) for m in _HOST_TOKEN.finditer(q)
                     if not re.fullmatch(r"\d+%?", m.group(0))), None)
        if _CAPACITY.search(q):
            return "get_capacity_risk", {"hostname": host} if host else {}
        if host and _STATUS.search(q) and not _WORST.search(q):
            return "get_host_status", {"hostname": host}
        if host:
            return "get_host_alerts", {"hostname": host}
        args: dict[str, Any] = {"limit": 10}
        m = re.search(r"\b(\d{1,2})\b", q)
        if m and 1 <= int(m.group(1)) <= 50:
            args["limit"] = int(m.group(1))
        if re.search(r"critical|disaster|أخطر|اخطر", q, re.I):
            args["severity_min"] = 4
        return "get_active_alerts_summary", args

    @staticmethod
    def _answer(prompt: str) -> str:
        start, end = prompt.find("{"), prompt.rfind("}")
        try:
            pack = json.loads(prompt[start:end + 1])
        except (ValueError, TypeError):
            return "SAMIX returned no usable data for this question."
        facts, unknowns = pack.get("facts", []), pack.get("unknowns", [])
        lines: list[str] = []
        if not facts:
            notes = [n.get("result") for n in pack.get("tool_notes", []) if n.get("result")]
            lines.append("No matching data in SAMIX for this question." + (" " + notes[0] if notes else ""))
        else:
            lines.append(f"Found {len(facts)} matching record(s) in SAMIX:")
            for f in facts[:12]:
                d = f.get("data", {})
                as_of = (f.get("as_of") or "")[11:16] or "-"
                src = f"({f.get('source_platform')}, {f.get('source_instance')}, as of {as_of})"
                if f["tool"] in ("get_host_alerts", "get_active_alerts_summary"):
                    lines.append(f"- [{d.get('severity_label')}] {d.get('host')}: {d.get('title')} {src}")
                elif f["tool"] == "get_host_status":
                    lines.append(f"- {d.get('hostname')} is {d.get('status')}, group {d.get('group') or '-'} {src}")
                else:
                    lines.append(f"- {d.get('hostname')} {d.get('resource')} {d.get('subject') or ''}: "
                                 f"{d.get('current_pct')}% now, {d.get('classification')} {src}")
        for u in unknowns:
            lines.append(f"- UNKNOWN: {u}")
        return "\n".join(lines)

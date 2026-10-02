"""Model-selection benchmark for SAMIX AI.

Runs 15 fixed questions (Arabic and English; real, missing and ambiguous
hosts; "is X monitored"; capacity) through the full ask flow and prints one
row each: tools called, rounds, validation, latency. Re-run with another
model to compare:

    python scripts/ai_benchmark.py --model qwen2.5:7b-instruct
    python scripts/ai_benchmark.py --model llama3.1:8b --ollama-url http://gpu-box:11434
    python scripts/ai_benchmark.py --mock            # seed demo data in a throwaway DB first
    python scripts/ai_benchmark.py --fake            # no Ollama: the deterministic fake model

By default it uses the database in your .env (so the hosts in the questions
should exist there - edit QUESTIONS to match your estate). --mock builds a
temporary SQLite database from the mock collectors and the forecast job, so
the run is self-contained and repeatable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QUESTIONS = [
    # valid hosts (mock estate names; edit for a live estate)
    "فيه مشاكل ايه على web-01؟",
    "What problems are on db-02?",
    "db-01 حالته ايه دلوقتي؟",
    "Is web-02 monitored, and by which tools?",
    "cache-01 متراقب من Dynatrace ولا لا؟",
    # informal / partial / ambiguous names
    "any alerts on web?",
    "ايه اخبار الـ db",
    "status of app-02",
    # nonexistent hosts
    "what problems are on MW10?",
    "فيه مشاكل على core-sw-99؟",
    # estate-wide
    "أخطر 5 alerts شغالة دلوقتي",
    "What is the worst thing happening right now?",
    # capacity
    "Which disks reach 90% within 30 days?",
    "ايه السيرفرات اللي الهارد بتاعها هيخلص قريب؟",
    "capacity risk for web-01",
]


def _seed_mock_db() -> None:
    """Point the app at a temp SQLite file and fill it from the mock collectors."""
    tmp = Path(tempfile.mkdtemp(prefix="samix-ai-bench-")) / "bench.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
    os.environ["MOCK_MODE"] = "true"
    os.environ["ENABLED_COLLECTORS"] = "zabbix,dynatrace,nnmi"
    from app.config import get_settings
    get_settings.cache_clear()
    from app.db import SessionLocal, init_db
    init_db()
    from app.collectors.dynatrace import DynatraceCollector
    from app.collectors.nnmi import NnmiCollector
    from app.collectors.zabbix import ZabbixCollector
    from app.forecast import run_forecast
    from app.servers import MOCK_SERVERS
    settings = get_settings()
    db = SessionLocal()
    try:
        for cfg in MOCK_SERVERS:
            cls = {"zabbix": ZabbixCollector, "dynatrace": DynatraceCollector, "nnmi": NnmiCollector}[cfg.platform]
            cls(cfg, settings).run(db)
        run_forecast(db)
    finally:
        db.close()
    print(f"seeded mock estate into {tmp}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="override AI_MODEL for this run")
    ap.add_argument("--ollama-url", help="override OLLAMA_URL for this run")
    ap.add_argument("--fake", action="store_true", help="use the deterministic fake model (no Ollama)")
    ap.add_argument("--mock", action="store_true", help="seed a throwaway DB from the mock collectors first")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")
    args = ap.parse_args()

    if args.mock:
        _seed_mock_db()

    from app.config import get_settings
    settings = get_settings()
    if args.model:
        settings.ai_model = args.model
    if args.ollama_url:
        settings.ollama_url = args.ollama_url
    from app.db import init_db
    init_db()

    from app.ai.gateway import Gateway
    if args.fake:
        from app.ai.fake import FakeLLM
        gw = Gateway(settings, llm=FakeLLM())
    else:
        gw = Gateway(settings)
        health = gw.health()
        if not health["ok"]:
            print(f"Ollama is not reachable at {settings.ollama_url} ({health['error']}). "
                  "Start it, pass --ollama-url, or use --fake.", file=sys.stderr)
            return 2
        if not health["model_present"]:
            print(f"Model {settings.ai_model} is not pulled on {settings.ollama_url}. "
                  f"Run: ollama pull {settings.ai_model}", file=sys.stderr)
            return 2

    rows = []
    for q in QUESTIONS:
        t0 = time.monotonic()
        r = gw.ask(q, user="benchmark")
        rows.append({
            "question": q,
            "tools": ", ".join(f"{t['name']}({'ok' if t['ok'] else 'ERR'}:{t['rows']})" for t in r.tools) or "-",
            "rounds": r.rounds,
            "validation": "error" if r.error else ("pass" if r.validation.passed else "FAIL"),
            "facts": len(r.pack.facts),
            "unknowns": len(r.pack.unknowns),
            "latency_ms": r.latency_ms,
            "trace_id": r.trace_id,
            "error": r.error,
            "problems": r.validation.problems if not r.error else [],
            "wall_ms": int((time.monotonic() - t0) * 1000),
        })
        print(".", end="", flush=True)
    print("\n")

    if args.json:
        print(json.dumps({"model": gw.llm.model, "results": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"model: {gw.llm.model}   url: {settings.ollama_url}   rounds max: {settings.ai_max_tool_rounds}\n")
    w_q = max(len(r["question"]) for r in rows)
    w_t = max(len(r["tools"]) for r in rows)
    print(f"{'question':<{w_q}}  {'tools':<{w_t}}  rnd  valid  facts  unk  latency")
    print("-" * (w_q + w_t + 40))
    for r in rows:
        print(f"{r['question']:<{w_q}}  {r['tools']:<{w_t}}  {r['rounds']:>3}  {r['validation']:<5}  "
              f"{r['facts']:>5}  {r['unknowns']:>3}  {r['latency_ms']:>6} ms")
    passed = sum(1 for r in rows if r["validation"] == "pass")
    avg = sum(r["latency_ms"] for r in rows) / len(rows)
    print(f"\nvalidation pass: {passed}/{len(rows)}   mean latency: {avg:.0f} ms")
    failures = [r for r in rows if r["validation"] != "pass"]
    if failures:
        print("\nnot passed:")
        for r in failures:
            why = r["error"] or "; ".join(r["problems"][:3])
            print(f"  [{r['trace_id']}] {r['question']}\n      {why}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

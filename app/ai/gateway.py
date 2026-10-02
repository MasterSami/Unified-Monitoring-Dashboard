"""Role 1 - the gateway. The only way in.

Authenticates (the Runbook session is SAMIX's one login), rate-limits per
user, validates the question, mints a trace_id, and runs the ask flow:

    1. model call #1 with the tool definitions - it picks tools
    2. the broker runs each call (allow-list, schema, timeout, row cap)
    3. up to AI_MAX_TOOL_ROUNDS of that, then the answer is forced
    4. the evidence pack is built (FACT / UNKNOWN, freshness)
    5. model call #2: answer ONLY from the pack
    6. the validator checks the answer against the pack
    7. the audit row is written

Everything a future split needs is already a seam: ``Gateway`` takes the
model and the broker as constructor arguments.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.runbook_auth import COOKIE_NAME, read_token

from . import prompts
from .audit import record_audit
from .evidence import EvidencePack, Validation, build_pack, freshness_by_instance, freshness_line, validate_answer
from .llm import ChatModel, LLMError, OllamaClient, tool_calls
from .tools import ToolArgumentError, ToolBroker, ToolResult, UnknownToolError, ollama_tool_definitions

logger = logging.getLogger("ai.gateway")

MAX_QUESTION_CHARS = 2000


class GatewayError(Exception):
    """A refused request: carries the HTTP status the router should return."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RateLimiter:
    """Sliding one-minute window per user, in memory. Good enough for one
    process; a shared store is the first thing a split-out gateway adds."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, int(per_minute))
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                wait = int(60 - (now - q[0])) + 1
                raise GatewayError(429, f"Too many questions; try again in {wait}s (limit {self.per_minute}/min).")
            q.append(now)


@dataclass
class AskResult:
    trace_id: str
    user: str
    question: str
    answer: str
    validation: Validation
    pack: EvidencePack
    tools: list[dict[str, Any]]
    rounds: int
    model: str
    latency_ms: int
    freshness_line: str
    error: str | None = None
    #: What the model did in each round, for the step indicator and the audit.
    steps: list[str] = field(default_factory=list)

    @property
    def validated(self) -> bool:
        return self.validation.passed and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "answer": self.answer,
            "validation": {"passed": self.validation.passed, "problems": self.validation.problems},
            "evidence": self.pack.to_dict(),
            "tools": self.tools,
            "rounds": self.rounds,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "freshness": self.freshness_line,
            "error": self.error,
        }


@dataclass
class Job:
    """An in-flight question, so the page can show which tool is running."""

    trace_id: str
    question: str
    user: str
    started: float = field(default_factory=time.monotonic)
    steps: list[str] = field(default_factory=list)
    result: AskResult | None = None
    error: GatewayError | None = None

    @property
    def done(self) -> bool:
        return self.result is not None or self.error is not None


_JOB_TTL_SECONDS = 600


class Gateway:
    def __init__(self, settings: Settings, llm: ChatModel | None = None, broker: ToolBroker | None = None) -> None:
        self.settings = settings
        self.llm: ChatModel = llm or OllamaClient(
            settings.ollama_url, settings.ai_model, float(settings.ai_timeout_seconds)
        )
        self.broker = broker or ToolBroker(settings)
        self.limiter = RateLimiter(settings.ai_rate_limit_per_min)
        self.max_rounds = max(1, int(settings.ai_max_tool_rounds))
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()

    # --- entry checks -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enable_ai)

    def identify(self, request: Request) -> str | None:
        """The Runbook-authenticated user, or - when login is not required -
        a stable per-client label so the rate limit still has a key."""
        user = read_token(self.settings, request.cookies.get(COOKIE_NAME))
        if user:
            return user
        if self.settings.ai_require_login:
            return None
        return f"anon:{request.client.host if request.client else 'local'}"

    def require_user(self, request: Request) -> str:
        if not self.enabled:
            raise GatewayError(404, "SAMIX AI is disabled (AI_ENABLED=false).")
        user = self.identify(request)
        if user is None:
            raise GatewayError(401, "Sign in to the Runbook to use SAMIX AI.")
        return user

    def validate_question(self, question: str) -> str:
        q = " ".join((question or "").split())
        if not q:
            raise GatewayError(422, "Ask something first.")
        if len(q) > MAX_QUESTION_CHARS:
            raise GatewayError(422, f"Question is too long (max {MAX_QUESTION_CHARS} characters).")
        return q

    # --- the ask flow -------------------------------------------------------

    def ask(self, question: str, user: str, trace_id: str | None = None,
            on_step: Callable[[str], None] | None = None) -> AskResult:
        trace_id = trace_id or uuid.uuid4().hex[:16]
        started = time.monotonic()
        steps: list[str] = []

        def step(text: str) -> None:
            steps.append(text)
            if on_step:
                on_step(text)

        tool_log: list[dict[str, Any]] = []
        results: list[ToolResult] = []
        rounds = 0
        error: str | None = None
        answer = ""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": f"{question}\n\n{prompts.TOOL_ROUND_HINT}"},
        ]
        tool_defs = ollama_tool_definitions()

        try:
            while rounds < self.max_rounds:
                rounds += 1
                step(f"Thinking (round {rounds} of {self.max_rounds})")
                message = self.llm.chat(messages, tools=tool_defs)
                calls = tool_calls(message)
                if not calls:
                    break
                messages.append({"role": "assistant", "content": message.get("content") or "",
                                 "tool_calls": message.get("tool_calls")})
                for name, args in calls:
                    shown = ", ".join(f"{k}={v}" for k, v in args.items() if k != "_raw") or "no arguments"
                    step(f"Calling {name}({shown})")
                    result = self._run_tool(name, args, trace_id)
                    results.append(result)
                    tool_log.append({"name": name, "args": result.args, "ok": result.ok,
                                     "rows": len(result.rows), "truncated": result.truncated,
                                     "error": result.error, "ms": result.elapsed_ms})
                    step(f"{name}: {len(result.rows)} row(s)" if result.ok else f"{name}: {result.error}")
                    messages.append({"role": "tool", "content": json.dumps(result.for_model(), default=str, ensure_ascii=False)})
            # Rounds exhausted or the model stopped asking: build the pack.
            step("Packaging evidence")
            pack = build_pack(question, results, self._freshness())
            step("Composing the answer")
            final = self.llm.chat([
                {"role": "system", "content": prompts.SYSTEM_PROMPT},
                {"role": "user", "content": prompts.ANSWER_PROMPT.format(pack=pack.prompt_json(), question=question)},
            ])
            answer = (final.get("content") or "").strip()
        except LLMError as exc:
            error = str(exc)
            logger.warning("ai %s: %s", trace_id, error)
            pack = build_pack(question, results, self._freshness())
        except Exception as exc:  # noqa: BLE001 - never 500 the user out of their data
            error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("ai %s failed", trace_id)
            pack = build_pack(question, results, self._freshness())

        step("Validating the answer")
        validation = validate_answer(answer, pack) if not error else Validation(False, [error])
        latency_ms = int((time.monotonic() - started) * 1000)
        record_audit(
            trace_id=trace_id, user=user, question=question, tools=tool_log, rounds=rounds,
            model=self.llm.model, latency_ms=latency_ms,
            validation="error" if error else validation.label, answer_chars=len(answer), error=error,
        )
        return AskResult(
            trace_id=trace_id, user=user, question=question, answer=answer, validation=validation,
            pack=pack, tools=tool_log, rounds=rounds, model=self.llm.model, latency_ms=latency_ms,
            freshness_line=freshness_line(pack.freshness), error=error, steps=steps,
        )

    def _run_tool(self, name: str, args: dict[str, Any], trace_id: str) -> ToolResult:
        """Allow-list and schema refusals are results too: the model is told,
        the pack records an UNKNOWN, and the audit shows the attempt."""
        try:
            return self.broker.execute(name, args)
        except UnknownToolError as exc:
            logger.warning("ai %s: refused tool %r", trace_id, name)
            return ToolResult(name, dict(args), False, error=f"refused: {exc}")
        except ToolArgumentError as exc:
            return ToolResult(name, dict(args), False, error=str(exc))

    def _freshness(self) -> dict[str, dict[str, Any]]:
        db = SessionLocal()
        try:
            return freshness_by_instance(db)
        finally:
            db.close()

    # --- background jobs (for the step indicator) ---------------------------

    def start_job(self, question: str, user: str) -> Job:
        job = Job(trace_id=uuid.uuid4().hex[:16], question=question, user=user)
        with self._jobs_lock:
            self._prune_jobs()
            self._jobs[job.trace_id] = job

        def run() -> None:
            try:
                job.result = self.ask(question, user, trace_id=job.trace_id, on_step=job.steps.append)
            except GatewayError as exc:
                job.error = exc
            except Exception as exc:  # noqa: BLE001
                job.error = GatewayError(500, f"{exc.__class__.__name__}: {exc}")

        threading.Thread(target=run, name=f"ai-{job.trace_id}", daemon=True).start()
        return job

    def job(self, trace_id: str) -> Job | None:
        with self._jobs_lock:
            return self._jobs.get(trace_id)

    def _prune_jobs(self) -> None:
        cutoff = time.monotonic() - _JOB_TTL_SECONDS
        for tid in [t for t, j in self._jobs.items() if j.started < cutoff]:
            self._jobs.pop(tid, None)

    def health(self) -> dict[str, Any]:
        probe = getattr(self.llm, "health", None)
        info = probe() if callable(probe) else {"ok": True, "model_present": True, "error": None}
        info["model"] = self.llm.model
        info["url"] = getattr(self.llm, "base", "")
        return info


_gateway: Gateway | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> Gateway:
    """Process-wide gateway built from settings; tests swap it for one with a
    fake model via :func:`set_gateway`."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = Gateway(get_settings())
        return _gateway


def set_gateway(gateway: Gateway | None) -> None:
    global _gateway
    with _gateway_lock:
        _gateway = gateway


def utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")

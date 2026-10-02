"""Role 2 - the LLM client. Talks to a local Ollama and nothing else.

This is the only module in ``app.ai`` that opens a network connection, and
the only outbound call the assistant makes at runtime: ``OLLAMA_URL``. It
knows nothing about SAMIX data; it sends messages and tool definitions and
returns the model's message. Swapping the model is an env change
(``AI_MODEL``); swapping the runtime means replacing this one class.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

logger = logging.getLogger("ai.llm")


class LLMError(RuntimeError):
    """The model runtime could not be reached or returned something unusable."""


class ChatModel(Protocol):
    """What the gateway needs from a model: one ``chat`` call. The Ollama
    client below implements it; tests and the benchmark plug in a fake."""

    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> dict[str, Any]: ...


def tool_calls(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``[(tool name, arguments dict), ...]`` from a model message, tolerant of
    arguments arriving as a JSON string rather than an object (some models
    do that) and of malformed entries (skipped, not fatal)."""
    out: list[tuple[str, dict[str, Any]]] = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        if name:
            out.append((name, args if isinstance(args, dict) else {}))
    return out


class OllamaClient:
    """Ollama's ``/api/chat`` with tool calling, non-streaming."""

    def __init__(self, url: str, model: str, timeout_seconds: float = 60.0) -> None:
        self.base = url.rstrip("/")
        self.model = model
        self.timeout = timeout_seconds

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Deterministic answers are what an evidence-only assistant wants.
            "options": {"temperature": 0},
        }
        if tools:
            payload["tools"] = tools
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.base}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama not reachable at {self.base}: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            raise LLMError(
                f"Ollama has no model named '{self.model}'. Pull it with: ollama pull {self.model}"
            )
        if resp.status_code >= 400:
            raise LLMError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("Ollama returned a non-JSON response") from exc
        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMError("Ollama response had no message")
        return message

    def health(self) -> dict[str, Any]:
        """Is the runtime up, and is the configured model pulled? Never raises."""
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "model_present": False, "models": [], "error": f"{exc.__class__.__name__}"}
        present = any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        return {"ok": True, "model_present": present, "models": names, "error": None}

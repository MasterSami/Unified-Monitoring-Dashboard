"""AI analysis provider interface — Correlation Phase 7 (architecture
preparation only).

**No AI/ML runs anywhere in this codebase.** There is no LLM call, no ML
model, no embeddings, no vector database, no RAG pipeline, and this module
adds none of those either — it defines the SHAPE a future AI-assistance
layer would plug into, nothing more. :class:`AIAnalysisProvider` has no
concrete subclass in this repository; :func:`get_ai_provider` always
returns ``None``.

Why this exists now, doing nothing: the task's own future architecture
(README "Correlation - Phase 7") puts AI assistance strictly AFTER and
BESIDE the deterministic engine, never inside it —

    Deterministic Correlation Engine -> Correlated Incident -> AI Assistance
        (rules, topology, dependencies,        (similar incidents, pattern
         trace evidence, entity resolution)      discovery, RCA suggestion,
                                                   explanation)

An AI layer only ever CONSUMES what Phases 1-6 already produce
(:mod:`app.incident_history`'s structured export, in particular) — it is
never consulted to decide whether two events correlate, never a
prerequisite for the engine to run, and its absence (``get_ai_provider()``
returning ``None``, the only implementation today) must never change a
single deterministic result. Nothing in ``app.correlation_engine`` or
``app.incident_engine`` imports this module; grep for
``app.ai_provider`` outside this file and its own test and you will not
find one.

Methods are deliberately ID-based and DB-agnostic (no SQLAlchemy ``Session``
in the signature): a real implementation might be a local model reading
``app.incident_history.export_incident_history`` output, or a remote
service called over HTTP with that same export serialized as its payload.
Either way, it consumes the deterministic evidence Phases 1-6 already
structured — it does not get its own parallel path into the database.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SimilarIncident:
    """One historical incident judged similar to the one being analyzed.

    ``similarity_score`` and ``matched_on`` are defined by whichever
    concrete provider computes them (a feature-overlap score, an embedding
    distance, ...) — this dataclass fixes only the SHAPE of the answer, not
    the method.
    """

    incident_id: int
    similarity_score: float
    #: What the similarity is actually based on, in plain terms —
    #: e.g. ["root_cause_entity_type=database", "same_correlation_type",
    #: "same_normalized_problem_type"] — so a human can sanity-check a
    #: similarity claim the same way they can sanity-check a correlation.
    matched_on: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RootCauseSuggestion:
    """A SUGGESTION, not a verdict — never presented as more certain than
    app.incident_engine's own deterministic root_cause_candidates; see that
    module's own "never fabricate certainty" note, which applies here too.
    """

    entity_id: int
    confidence: float
    rationale: str
    #: Historical incidents this suggestion is grounded in, if any.
    based_on_incident_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Pattern:
    """A recurring shape the provider noticed across multiple incidents —
    e.g. "database connection pool exhaustion precedes API 500s within 2
    minutes, in 8 of the last 10 occurrences."
    """

    description: str
    incident_ids: list[int] = field(default_factory=list)
    #: The CorrelationSignal value this pattern centers on, if any.
    signal: str | None = None


@dataclass(frozen=True)
class CorrelationRuleSuggestion:
    """A candidate rule an operator could review and create via the
    EXISTING, unchanged app.correlation_rules.create_rule — this dataclass
    never creates a rule itself. ``suggested_conditions`` uses that same
    condition shape (a flat list of single-purpose objects), so accepting a
    suggestion is a direct, unmodified call into Phase 4, not a new rule
    format to support.
    """

    suggested_conditions: list[dict]
    rationale: str
    based_on_incident_ids: list[int] = field(default_factory=list)


class AIAnalysisProvider(ABC):
    """Abstract interface for a future AI-assistance layer.

    Not implemented anywhere in this codebase (task: "Do not implement the
    provider") — this class cannot even be instantiated directly (Python
    raises ``TypeError`` on any subclass that leaves an ``@abstractmethod``
    unimplemented, and there are no subclasses here at all). It exists so
    that WHEN a real provider is built later, it has one settled shape to
    implement against, and nothing in the deterministic engine needs to
    change to accommodate it.
    """

    @abstractmethod
    def find_similar_incidents(self, incident_id: int, *, limit: int = 5) -> list[SimilarIncident]:
        """Historical incidents most like this one, most similar first."""
        raise NotImplementedError

    @abstractmethod
    def suggest_root_cause(self, incident_id: int) -> RootCauseSuggestion | None:
        """A suggested root cause beyond (or reinforcing) the deterministic
        engine's own root_cause_candidates — never a replacement for them.
        """
        raise NotImplementedError

    @abstractmethod
    def detect_patterns(self, *, since: datetime | None = None) -> list[Pattern]:
        """Recurring shapes across the incident history, optionally bounded
        to incidents that started at/after ``since``.
        """
        raise NotImplementedError

    @abstractmethod
    def suggest_correlation_rule(self, *, incident_ids: list[int]) -> CorrelationRuleSuggestion | None:
        """A candidate app.correlation_rules rule inferred from the given
        incidents — for a human to review and create, never auto-applied.
        """
        raise NotImplementedError

    @abstractmethod
    def summarize_incident(self, incident_id: int) -> str:
        """A natural-language summary of one incident, for a human to read
        alongside — never in place of — its structured evidence.
        """
        raise NotImplementedError


def get_ai_provider() -> AIAnalysisProvider | None:
    """The configured AI provider, or ``None``.

    Always returns ``None`` today — no provider is implemented or
    registered anywhere in this codebase. Every call site that will ever
    consult this (there are none yet) must treat ``None`` as a completely
    normal, permanent-until-someone-builds-one answer, not an error: "AI
    must be optional" (task section 6) means the deterministic engine's
    correctness can never depend on this returning anything else.
    """
    return None

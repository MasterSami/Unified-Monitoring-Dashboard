# SAMIx AI PoC v0.1

## Current scope

This first step establishes a **read-only Dynatrace evidence tool contract**. It does not add an LLM, RAG, write action, or new network client.

## Tool

`get_dynatrace_problems(hostname, time_window_minutes, severity_filter, max_results)`

The tool consumes the existing Dynatrace collector through dependency injection. The collector remains the only component that owns source credentials and network access.

## Safety boundaries

- Read-only by contract.
- No credentials in the tool result.
- No raw API payload in model context.
- Host, time-window, severity, and result-limit validation.
- Explicit `SOURCE_UNAVAILABLE` result instead of guessing.
- Versioned structured evidence schema: `samix.ai.dynatrace-problems.v0.1`.
- Evidence is labelled `FACT` and includes source ID, timestamp, source instance, and freshness.

## Not included yet

- Local LLM runtime.
- MCP transport/server.
- General AI Gateway.
- RAG/vector database.
- Write operations.
- Autonomous remediation.

Those are intentionally deferred until the tool contract and authorization boundary are approved.

## Validation

- Compile check passed.
- New PoC tests passed.
- Existing AI readiness tests passed.
- Result: `16 passed`.

## Gateway boundary completed

`AIGateway` now provides the next security boundary. It is disabled by default,
requires an authenticated user with the `ai.read` scope, checks an optional
host allow-list, permits only `get_dynatrace_problems`, and emits an audit event
for both allowed and denied calls. It still invokes no LLM and exposes no HTTP
route.

The next small step is to connect this contract to SAMIx authentication through
a feature-flagged internal route. That route will remain disabled until the
application's identity source and host-level authorization policy are selected.

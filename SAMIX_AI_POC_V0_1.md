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

## Next small step

Add a disabled-by-default AI Gateway contract that accepts a user question, enforces the read-only tool allow-list, and returns the structured Dynatrace evidence without invoking an external model.

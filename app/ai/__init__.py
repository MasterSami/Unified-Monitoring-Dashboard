"""SAMIX AI - a read-only operations assistant over SAMIX's own data.

A modular monolith: five roles, one process, strict boundaries, so that each
module can later become its own service without the others noticing.

    gateway.py   role 1  entry point - auth, rate limit, validation, trace_id
    llm.py       role 2  Ollama client (the ONLY outbound network call)
    tools.py     role 3  tool broker - allow-list, schemas, timeouts, limits
    evidence.py  role 4  evidence pack (FACT / UNKNOWN) + output validator
    audit.py     role 5  audit log (ai_audit) and feedback (ai_feedback)
    prompts.py           the system prompt, kept editable in one place
    router.py            /ai page, /partials/ai/*, /api/v1/ai/*

The model never talks to Zabbix, Dynatrace, NNMi or SiteScope. It only sees
rows SAMIX already normalized, and it answers only from the evidence pack
those rows become.
"""

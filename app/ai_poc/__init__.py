"""Small, feature-flagged building blocks for the SAMIx AI PoC.

This package intentionally contains no LLM call and no network client of its
own. It consumes the existing read-only collector contract so authentication,
credentials, retries, and source behavior remain in one place.
"""

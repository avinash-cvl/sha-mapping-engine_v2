"""Agent -- synonym query-expansion for lexical candidate retrieval.

Called from flow.py's run_candidates task, once per unique source title,
before stage_candidates.lexical_search(). Generates alternate/synonym terms
for a title (e.g. "wash" -> "cleanser", "moisturizer" -> "lotion") that get
merged into the BM25 query text, widening recall on the lexical channel
only -- semantic search, scoring, and disposition are untouched.
"""
from __future__ import annotations

import json

from agents.llm_client import get_chat_model, invoke_and_audit

_SYSTEM_PROMPT = (
    "You expand a marketplace product title into alternate search terms for "
    "a keyword (lexical) search engine over a personal-care product "
    "catalogue. Give synonyms for the product-type/category words only "
    "(e.g. \"wash\" -> \"cleanser\", \"moisturizer\" -> \"lotion\", "
    "\"shampoo\" -> \"hair cleanser\") -- never invent brand names or pack "
    "sizes. Return at most 5 short terms, most useful first. Respond with "
    'strict JSON only: {"synonyms": [string, ...]}.'
)


def expand(title: str) -> tuple[list[str], dict[str, object]]:
    """Returns (synonym_terms, audit_entry). Empty list on any parse failure --
    callers fall back to the original title, never block retrieval on this."""
    model = get_chat_model()
    response_text, audit_entry = invoke_and_audit(
        model,
        [("system", _SYSTEM_PROMPT), ("human", title)],
        call_type="synonyms",
    )
    try:
        parsed = json.loads(response_text)
        terms = parsed.get("synonyms", [])
        return [t for t in terms if isinstance(t, str) and t.strip()], audit_entry
    except (json.JSONDecodeError, AttributeError):
        audit_entry["success"] = False
        return [], audit_entry

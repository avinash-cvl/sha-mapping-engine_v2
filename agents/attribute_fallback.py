"""Agent 2 -- attribute-extraction fallback (founding_doc.md section B.6
point 2).

Called from stage_attributes.py's flow.py wiring only when the rule-based
parser couldn't populate sub_brand/variant -- novel phrasing the rules
haven't seen. Every call returns an audit_entry shaped for audit.llm_call_log;
flow.py is responsible for actually writing it.
"""
from __future__ import annotations

import json

from agents.llm_client import get_chat_model, invoke_and_audit

_SYSTEM_PROMPT = (
    "You extract two fields from a marketplace product title: sub_brand "
    "(a named sub-line within the brand, or null) and variant (a "
    "flavour/scent/type qualifier, or null). Respond with strict JSON "
    'only: {"sub_brand": string|null, "variant": string|null}.'
)


def extract(title: str, brand: str) -> tuple[str | None, str | None, dict[str, object]]:
    """Returns (sub_brand, variant, audit_entry)."""
    model = get_chat_model()
    response_text, audit_entry = invoke_and_audit(
        model,
        [("system", _SYSTEM_PROMPT), ("human", f"brand: {brand}\ntitle: {title}")],
        call_type="attribute_fallback",
    )
    try:
        parsed = json.loads(response_text)
        return parsed.get("sub_brand"), parsed.get("variant"), audit_entry
    except (json.JSONDecodeError, AttributeError):
        audit_entry["success"] = False
        return None, None, audit_entry

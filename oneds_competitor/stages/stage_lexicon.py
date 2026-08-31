"""Stage 2 -- brand gate + Lexicon (governed acronym/normalization layer).

STATUS: the brand gate is implemented (SourceProduct.match_type is set in
stage_ingest.py's loaders). The governed Lexicon itself -- entry schema,
mining pipeline, runtime waterfall -- does not exist anywhere yet; there
was no precedent to adapt from in the Himalaya-SKU-Mapping prior art
either. Founding doc section B.3 has the full design.

expand() is a no-op passthrough for now so flow.py can call it unconditionally
without every other stage needing to handle "lexicon not implemented yet."
"""
from __future__ import annotations

from common.models import SourceProduct


def lookup_lexicon(token: str) -> str | None:
    """Tier-1 exact + tier-2 fuzzy (>=90%, tokens >=5 chars) lookup against
    config.token_normalization. Returns the canonical expansion, or None if
    the token isn't in the Lexicon (falls through unexpanded).

    TODO: implement the three-tier waterfall from founding_doc.md section B.3.
    Tier 3 (LLM fallthrough) belongs in agents/acronym_agent.py -- mining-time
    only, never called from here at runtime.
    """
    raise NotImplementedError("Lexicon runtime waterfall not built yet -- see founding_doc.md section B.3")


def expand(source: SourceProduct) -> SourceProduct:
    """Apply Lexicon expansion to a source row's tokens. Currently a no-op
    passthrough until lookup_lexicon() is implemented."""
    return source

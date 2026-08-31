"""Stage 7 -- recovery loop.

STATUS: no precedent anywhere in the Himalaya-SKU-Mapping prior art -- this
is genuinely new. Founding doc section B.2 Stage 7: frequency-mine
unresolved tokens from the unmatched pool into a ranked Lexicon backlog,
draft -> validate on a labelled sample -> steward-approve -> version new
entries, then re-run stages 2-6 on the unmatched pool ONLY (matched
records are never re-litigated -- section B.13 invariant).

Not called from flow.py yet -- wire it in once stage_lexicon.py is real,
since recovery only makes sense as a Lexicon-backlog cycle.
"""
from __future__ import annotations

from common.models import MatchResult


def mine_unresolved_tokens(unmatched: list[MatchResult]) -> dict[str, int]:
    """Frequency-count tokens appearing in unmatched/low-confidence source
    titles, as candidate input to agents.acronym_agent.

    TODO: implement per founding_doc.md section B.3's mining pipeline
    (detect -> align -> bucket).
    """
    raise NotImplementedError("Recovery loop not built yet -- see founding_doc.md section B.2 Stage 7")


def rerun_unmatched(unmatched: list[MatchResult]) -> list[MatchResult]:
    """Re-run stages 2-6 on the unmatched pool only, after a Lexicon backlog
    cycle has landed new entries. Currently a no-op passthrough."""
    return unmatched

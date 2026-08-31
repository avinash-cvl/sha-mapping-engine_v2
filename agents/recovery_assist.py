"""Agent 4 -- recovery-loop assist (founding_doc.md section B.6 point 4).

Optional, scoped resolution of mined tokens during Stage 7 recovery,
orchestrator-controlled rather than free-running.

STATUS: stub. Depends on stage_recovery.py's mining step, which doesn't
exist yet either.
"""
from __future__ import annotations


def assist(mined_tokens: dict[str, int]) -> dict[str, str]:
    raise NotImplementedError(
        "Recovery loop doesn't exist yet to call this from -- see stage_recovery.py"
    )

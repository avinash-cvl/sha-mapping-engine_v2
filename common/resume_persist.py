"""CLI shim for flow.resume_persist_flow -- same recovery flow, triggered
via `docker exec` instead of the Prefect UI. See flow.py's
resume_persist_flow docstring for what this actually does and its one
known gap (LLM audit-log entries for the original run aren't recoverable).

Usage: python3 resume_persist.py <run_id>
"""
from __future__ import annotations

import sys

from flow import resume_persist_flow

if __name__ == "__main__":
    resume_persist_flow(sys.argv[1])

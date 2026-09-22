"""The parser is checked against the real logs on disk, not a fixture.

Every bug it has had came from a shape a handwritten sample would not have
contained: WORKER FAILED logged twice with only the second carrying the
error, tracebacks written as bare continuation lines, a run banner whose
regex swallowed the first group's scope.
"""
from __future__ import annotations

import glob
import os
import pathlib

import pytest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import log_parse

LOGS = sorted(glob.glob(str(pathlib.Path(__file__).parent.parent / "logs" / "*.log")),
              key=os.path.getsize, reverse=True)


@pytest.fixture(scope="module")
def biggest():
    if not LOGS:
        pytest.skip("no logs on disk")
    return log_parse.parse(
        open(LOGS[0], encoding="utf-8", errors="replace").read()
    )


def test_reconstructs_the_group_structure(biggest):
    """Steps 1-5 run once; steps 6+ repeat per group. A flat step list would
    describe a 97-group run as sixteen steps."""
    assert biggest["totals"]["groups"] > 1
    g = biggest["groups"][0]
    assert g["category"] and g["subcategory"]
    assert g["batch_size"] > 0


def test_failures_carry_their_reason(biggest):
    """WORKER FAILED is logged twice and only the second carries error=.
    Keeping the first made every failure look reasonless, so a run of pure
    deadlocks reported all_failures_retryable: false."""
    failures = biggest["failures"]
    if not failures:
        pytest.skip("this run had no failures")
    assert all(f["sku"] for f in failures)
    with_reason = [f for f in failures if f["error"]]
    assert with_reason, "every failure lost its error text"
    assert any(f["retryable"] for f in with_reason)


def test_a_failure_is_not_recorded_twice(biggest):
    skus = [f["sku"] for f in biggest["failures"]]
    assert len(skus) == len(set(skus))


def test_warnings_are_kept_with_their_group(biggest):
    """Warnings name the gate target that rejected a SKU -- the trail back to
    why a match looks wrong, not noise above the errors."""
    warnings = biggest["warnings"]
    if not warnings:
        pytest.skip("this run had no warnings")
    assert all("level" in w and "message" in w for w in warnings)


def test_run_header_does_not_swallow_a_group_scope(biggest):
    """Step 6 logs 'category=X | subcategory=Y | batch_size=N' on one line.
    An unanchored header regex absorbed it and reported the first group's
    scope as the whole run's."""
    cfg = biggest["setup"]["config"]
    for key in ("category", "subcategory"):
        if cfg.get(key):
            assert "|" not in cfg[key]
            assert "subcategory=" not in cfg[key]


def test_totals_agree_with_the_groups(biggest):
    assert biggest["totals"]["successful"] == sum(
        g["successful"] for g in biggest["groups"]
    )
    assert biggest["totals"]["failed"] == sum(g["failed"] for g in biggest["groups"])


@pytest.mark.parametrize("path", LOGS[:6])
def test_unparsed_stays_low_across_real_logs(path):
    """Unparsed means 'the format may have drifted'. It is on screen for that
    reason, so it must not be diluted by traceback lines, DEBUG tracing or
    verbose progress -- each of which is counted separately."""
    d = log_parse.parse(open(path, encoding="utf-8", errors="replace").read())
    c = d["counts"]
    if c["lines"] < 50:
        pytest.skip("too small to be meaningful")
    assert c["unparsed"] / c["lines"] < 0.20, (
        f"{os.path.basename(path)}: {c['unparsed']}/{c['lines']} unparsed"
    )


def test_empty_and_malformed_input_do_not_raise():
    assert log_parse.parse("")["totals"]["groups"] == 0
    assert log_parse.parse("not a log\nat all\n")["counts"]["traceback"] == 2

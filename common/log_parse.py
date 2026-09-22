"""Turn a run log into the structure it already has.

batch_flow logs in a fixed grammar -- `timestamp | LEVEL | thread | message` --
and the messages follow stable patterns. What it does NOT log is the shape of
the run, which a reader has to reconstruct: steps 1-5 run once to set up, then
steps 6 onward repeat per category/sub-category group. A 97-group run is
therefore not sixteen steps, it is five steps and ninety-seven repetitions,
and a flat list of step markers describes it wrongly.

The numbers are the argument for parsing at all. One real run:

    21,980 lines
     7,642 HTTP request chatter          (35%)
     2,044 `=====` separator rules        (9%)
     2,551 per-SKU WORKER END lines      (12%)
       341 warnings and errors          (1.5%)

Tailing the last 40 lines shows whichever group happened to finish last. The
341 lines that matter are unreachable without reading the other 21,639.

Every line that does not match a known pattern is counted, not dropped. A
structured view that silently discards the one unexpected line is worse than
a raw one, because it looks complete.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# 2026-09-21 14:30:06,704 | INFO | MainThread | message
LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s*\|\s*"
    r"(?P<level>\w+)\s*\|\s*(?P<thread>[^|]+?)\s*\|\s*(?P<msg>.*)$"
)

# Rules and blank banner lines carry no information; they are 9% of the file.
NOISE = re.compile(r"^[=\-]+$|^$")

STEP_START = re.compile(r"^BATCH FLOW - STEP (?P<step>[0-9]+[A-C]?)$")
STEP_DONE = re.compile(r"^STEP (?P<step>[0-9]+[A-C]?) COMPLETED(?:\s*[-|]\s*(?P<detail>.*))?$")
GROUP_SCOPE = re.compile(
    r"^STEP 6 COMPLETED \| category=(?P<category>.*?) \| "
    r"subcategory=(?P<subcategory>.*?) \| batch_size=(?P<batch>\d+)$"
)
GROUP_DONE = re.compile(r"^GROUP COMPLETED \| \((?P<scope>.*)\)$")
ELIGIBLE = re.compile(r"^Eligible master records for BM25: (?P<n>\d+)$")
SUCCESSFUL = re.compile(r"^Successful\s*:\s*(?P<n>\d+)$")
FAILED_N = re.compile(r"^Failed\s*:\s*(?P<n>\d+)$")
WORKER_FAILED = re.compile(
    r"^WORKER FAILED \| source_id=(?P<sid>\S+) \| SKU=(?P<sku>\S+?)"
    r"(?: \| error=(?P<error>.*))?$"
)
CROSSWALK = re.compile(r"^Crosswalk short-circuit: (?P<n>\d+) / (?P<total>\d+)")
HTTP = re.compile(r"^HTTP Request:")

# Verbose progress the structured view has no use for: sample rows echoed
# back, per-node lookup tallies, and banner lines whose content the parsed
# fields already carry. Recognised explicitly rather than left to fall
# through, so "unparsed" keeps meaning "the format may have drifted" instead
# of "there is a lot of detail in here".
VERBOSE = re.compile(
    r"^\d+\. (id=|1DS=)"                       # echoed sample rows
    r"|^-> master="                              # per-row category resolution
    r"|^(Master group|Source group|Mapping records found|"
    r"TOTAL ELIGIBLE MASTER RECORDS|PROCESSING GROUP|"
    r"Source records (to complete|marked COMPLETED)|"
    r"Distinct PENDING|Total (PENDING|master records)|"
    r"PENDING records retrieved|BM25 (index|corpus)|"
    r"Submitted \d+|STARTING PARALLEL|batch=|"
    r"master_by_code entries|Master pack sizes parsed|"
    r"MasterProduct objects created|Mapping table resolved|"
    r"Filter \||All source records|No PENDING source records)"
)
WORKER_END = re.compile(r"^WORKER (END|START)\b")
# The run banner writes "key       = value" with padding. Step 6 writes
# "category=X | subcategory=Y | batch_size=N" on one line, so the value is
# anchored to stop at a pipe -- otherwise the banner absorbs a group's scope
# and reports it as the whole run's.
RUN_HEADER = re.compile(
    r"^(?P<key>run_id|source_table|master_table|category|subcategory|"
    r"batch_size|max_workers|sku filter|log_file)\s+=\s*(?P<value>[^|]*)$"
)

# A deadlock is transient and retryable; a genuine fault is not. Saying so is
# the difference between "retry this" and "investigate this", and the console
# got that wrong for a whole day.
DEADLOCK = re.compile(r"40001|deadlock", re.I)


@dataclass
class Group:
    category: str | None = None
    subcategory: str | None = None
    batch_size: int = 0
    eligible_master: int | None = None
    crosswalk_resolved: int = 0
    successful: int = 0
    failed: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    steps: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float | None:
        if self.started_at and self.ended_at:
            return round((self.ended_at - self.started_at).total_seconds(), 1)
        return None

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "subcategory": self.subcategory,
            "batch_size": self.batch_size,
            "eligible_master": self.eligible_master,
            "crosswalk_resolved": self.crosswalk_resolved,
            "successful": self.successful,
            "failed": self.failed,
            "seconds": self.seconds,
            "steps": self.steps,
            "warnings": self.warnings[:20],
            "status": ("failed" if self.failed and not self.successful
                       else "partial" if self.failed
                       else "ok"),
        }


def parse(text: str, max_failures: int = 500) -> dict:
    """Parse a run log into setup, groups and failures.

    Streams line by line rather than loading a structure per line: these files
    reach 22k lines and the per-SKU chatter is 12% of them, none of which needs
    to be kept.
    """
    setup: dict[str, object] = {"steps": [], "config": {}}
    groups: list[Group] = []
    failures: list[dict] = []
    warnings: list[dict] = []
    counts = {"lines": 0, "http": 0, "worker": 0, "noise": 0,
              "verbose": 0, "debug": 0, "traceback": 0, "unparsed": 0}

    seen_failures: dict[str, dict] = {}
    current: Group | None = None
    in_setup = True
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for raw in text.splitlines():
        counts["lines"] += 1
        m = LINE.match(raw)
        if not m:
            # A logger.exception() writes the traceback as bare continuation
            # lines with no timestamp -- 190 failures produced 7,550 of them,
            # a third of the file. They belong to the WORKER FAILED line above
            # and are counted as such, not as lines the parser failed to
            # understand: conflating the two made "unparsed" read 34% and hid
            # whether the format had actually drifted.
            counts["traceback"] += 1
            continue

        level, msg = m.group("level"), m.group("msg").strip()
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = None
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        # DEBUG is per-SKU tracing from a --debug run: 24,208 lines in one
        # 24,762-line file. It is diagnostic detail for one investigator, not
        # part of the run's shape, and counting it as unparsed would suggest
        # the format had drifted when nothing had changed.
        if level == "DEBUG":
            counts["debug"] += 1
            continue

        if NOISE.match(msg):
            counts["noise"] += 1
            continue
        if HTTP.match(msg):
            counts["http"] += 1
            continue
        if WORKER_END.match(msg):
            counts["worker"] += 1
            continue
        if VERBOSE.match(msg):
            counts["verbose"] += 1
            continue

        # --- run header (only before the first group)
        if in_setup and (hm := RUN_HEADER.match(msg)):
            setup["config"][hm.group("key").strip()] = hm.group("value").strip()
            continue

        # --- a new group begins at STEP 6's scope line
        if gm := GROUP_SCOPE.match(msg):
            in_setup = False
            current = Group(
                category=gm.group("category"),
                subcategory=gm.group("subcategory"),
                batch_size=int(gm.group("batch")),
                started_at=ts,
            )
            groups.append(current)
            continue

        if sm := STEP_START.match(msg):
            # Step 6 opens a group but its scope is only known one line later,
            # so the group is created there, not here.
            if sm.group("step") == "6":
                in_setup = False
            continue

        if dm := STEP_DONE.match(msg):
            entry = {"step": dm.group("step"), "detail": (dm.group("detail") or "").strip()}
            (current.steps if current else setup["steps"]).append(entry)
            continue

        if current is not None:
            if em := ELIGIBLE.match(msg):
                current.eligible_master = int(em.group("n"))
                continue
            if cm := CROSSWALK.match(msg):
                current.crosswalk_resolved = int(cm.group("n"))
                continue
            if sm2 := SUCCESSFUL.match(msg):
                current.successful = int(sm2.group("n"))
                continue
            if fm := FAILED_N.match(msg):
                current.failed = int(fm.group("n"))
                continue
            if GROUP_DONE.match(msg):
                current.ended_at = ts
                current = None
                continue

        if wf := WORKER_FAILED.match(msg):
            error = (wf.group("error") or "").strip()
            sku = wf.group("sku").strip("'\"")

            # Logged twice per failure: once by the worker thread as it dies,
            # once by MainThread when it collects the future -- and only the
            # second carries error=. Taking the first meant every failure
            # looked reasonless, so a run of pure deadlocks reported
            # "all_failures_retryable: false" and read as an engine fault.
            existing = seen_failures.get(sku)
            if existing is not None:
                if error and not existing["error"]:
                    existing["error"] = error[:300]
                    existing["retryable"] = bool(DEADLOCK.search(error))
                continue

            if len(failures) < max_failures:
                entry = {
                    "sku": wf.group("sku").strip("'\""),
                    "source_id": wf.group("sid"),
                    "category": current.category if current else None,
                    "subcategory": current.subcategory if current else None,
                    "error": error[:300],
                    # Transient lock contention, not a matching fault. The
                    # remedy is a retry at lower concurrency, and saying so
                    # here is what stops it reading as an engine failure.
                    "retryable": bool(DEADLOCK.search(error)),
                }
                failures.append(entry)
                seen_failures[sku] = entry
            continue

        if level in ("WARNING", "ERROR"):
            entry = {
                "level": level,
                "message": msg[:300],
                "category": current.category if current else None,
                "subcategory": current.subcategory if current else None,
            }
            warnings.append(entry)
            if current:
                current.warnings.append(msg[:200])
            continue

        counts["unparsed"] += 1

    total_ok = sum(g.successful for g in groups)
    total_failed = sum(g.failed for g in groups)
    deadlocks = sum(1 for f in failures if f["retryable"])

    return {
        "setup": setup,
        "groups": [g.as_dict() for g in groups],
        "failures": failures[:max_failures],
        "warnings": warnings[:200],
        "totals": {
            "groups": len(groups),
            "successful": total_ok,
            "failed": total_failed,
            "groups_with_failures": sum(1 for g in groups if g.failed),
            "deadlocks": deadlocks,
            # A run whose failures are all deadlocks needs a retry, not an
            # investigation. Deciding that here keeps the judgement in one
            # place instead of in every caller.
            "all_failures_retryable": bool(failures) and deadlocks == len(failures),
            "seconds": (round((last_ts - first_ts).total_seconds(), 1)
                        if first_ts and last_ts else None),
        },
        # Kept visible rather than swallowed: if the log format drifts, this
        # number moves and the structure can be trusted less, which is
        # something a reader should be able to see.
        "counts": counts,
    }

"""Save and load each stage's output as plain JSON, one file per stage per
run -- so you can open a file and see exactly what a stage produced,
without needing the Prefect UI or a debugger.

Files land at data/intermediate/<run_id>/<stage_name>.json. flow.py calls
save() at the end of every @task; nothing else needs to call this module.

Prefect also has a built-in result-persistence feature
(@task(persist_result=True)) that does something similar internally, but
it pickles Python objects rather than writing readable JSON -- this module
is deliberately the more accessible of the two. Use both if you want: they
don't conflict.
"""
from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

import common.config as C


def _path(run_id: str, stage_name: str) -> str:
    return os.path.join(C.CHECKPOINT_DIR, run_id, f"{stage_name}.json")


def _to_serializable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def save(run_id: str, stage_name: str, records: list[Any]) -> None:
    """Write a stage's output list to data/intermediate/<run_id>/<stage_name>.json."""
    path = _path(run_id, stage_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    serializable = [_to_serializable(record) for record in records]
    with open(path, "w") as f:
        json.dump(serializable, f, indent=2, default=str)


def load(run_id: str, stage_name: str) -> list[dict[str, Any]]:
    """Read back a previously-saved stage checkpoint as plain dicts."""
    with open(_path(run_id, stage_name)) as f:
        result: list[dict[str, Any]] = json.load(f)
        return result


def exists(run_id: str, stage_name: str) -> bool:
    return os.path.exists(_path(run_id, stage_name))

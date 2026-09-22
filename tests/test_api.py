"""The API reads through the same code the engine runs on."""
from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore")


def test_scope_returns_a_leg_per_pipeline_never_a_sum(client):
    """Two figures, not one. A single total is what hid a 2x duplicate
    ingest: 552 rows looked plausible where 274 + 274 would not have."""
    r = client.post("/api/engine/scope", json={
        "channel": "zepto", "category": "lip makeup", "subcategory": "lip balms",
    })
    assert r.status_code == 200
    legs = {x["engine"]: x for x in r.json()["legs"]}
    assert set(legs) == {"himalaya", "competitor"}
    assert legs["himalaya"]["scoped_total"] == 4
    assert legs["competitor"]["scoped_total"] == 274


def test_scope_honours_a_selected_pipeline(client):
    """Selecting one side must not price both, or the preview describes a
    run that is not the one about to start."""
    r = client.post("/api/engine/scope",
                    json={"channel": "zepto", "engine": "competitor"})
    assert [l["engine"] for l in r.json()["legs"]] == ["competitor"]


def test_scope_estimate_is_labelled_as_one(client):
    est = client.post("/api/engine/scope", json={"channel": "zepto"}).json()["estimate"]
    assert "llm_calls" in est and "seconds" in est
    assert "measured" in est["basis"]


@pytest.mark.parametrize("payload,expected", [
    ({"channel": "nope"}, 400),
    ({"channel": "zepto", "engine": "sideways"}, 400),
])
def test_bad_scope_is_rejected(client, payload, expected):
    assert client.post("/api/engine/scope", json=payload).status_code == expected


def test_channels_lists_real_taxonomy(client):
    """Driven from the data so the client never hardcodes a taxonomy that
    changes on ingest."""
    d = client.get("/api/engine/channels").json()
    assert set(d) == {"amazon", "blinkit", "swiggy", "zepto"}
    assert "lip makeup" in d["zepto"]


def test_run_history_and_detail(client):
    runs = client.get("/api/engine/runs?limit=5").json()
    assert isinstance(runs, list)
    if not runs:
        pytest.skip("no runs recorded yet")

    rid = runs[0]["execution_id"]
    d = client.get(f"/api/engine/runs/{rid}").json()
    assert d["run"]["execution_id"] == rid
    # The config snapshot is what makes a later comparison interpretable.
    assert len(d["config"]) >= 16


def test_unknown_run_is_404(client):
    r = client.get("/api/engine/runs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_failed_preview_reads_only(client):
    r = client.post("/api/engine/failed/preview",
                    json={"channel": "zepto", "engine": "competitor"})
    assert r.status_code == 200
    assert r.json()["rows_reset"] == 0


def test_results_carry_the_group_a_row_belongs_to(client):
    """A run spans one category but many sub-category groups -- 14 in one
    51-SKU run -- so a row without them cannot be placed."""
    d = client.get("/api/engine/results?channel=zepto&limit=5").json()
    if not d["rows"]:
        pytest.skip("no results yet")
    row = d["rows"][0]
    assert row["category"] and row["subcategory"]
    assert row["engine"] in ("himalaya", "competitor")


def test_results_filter_server_side(client):
    """151k rows on amazon competitor: the client cannot filter these."""
    d = client.get("/api/engine/results?channel=zepto&status=AutoMatch&limit=5").json()
    assert all(r["status"] == "AutoMatch" for r in d["rows"])


def test_group_summary_rates_decided_rows_only(client):
    """Counting PENDING in the denominator makes a half-finished category
    look weak when it is merely unfinished."""
    groups = client.get("/api/engine/results/groups?channel=zepto").json()
    if not groups:
        pytest.skip("nothing processed yet")
    for g in groups:
        if g["automatch_rate"] is not None:
            assert 0.0 <= g["automatch_rate"] <= 1.0


def test_gate_warnings_surface_starved_nodes(client):
    """A gate onto a one-row node returns that row whatever the listing
    says -- how every serum came back as the same SERUM FLUID."""
    w = client.get("/api/engine/gate-warnings?limit=10").json()
    assert w, "expected starved targets; 86 of 191 hold <= 3 rows"
    assert w[0]["rows"] <= w[-1]["rows"]     # worst first


def test_config_exposes_weights_as_a_set(client):
    """They are a blend: one raised in isolation silently changes what every
    other signal is worth."""
    w = client.get("/api/engine/config").json()["weights"]
    assert len(w["values"]) == 5
    assert abs(w["total"] - 1.0) < 0.001


def test_config_locks_the_portal_contract(client):
    settings = {s["key"]: s for s in client.get("/api/engine/config").json()["settings"]}
    assert settings["TOP_N_OUTPUT"]["locked"] is True


def test_plan_launches_nothing(client):
    """The confirmation dialog must be a read, or it is not a confirmation."""
    body = {"channel": "zepto", "engine": "competitor",
            "category": "lip makeup", "subcategory": "lip balms", "workers": 4}
    first = client.post("/api/engine/plan", json=body).json()
    second = client.post("/api/engine/plan", json=body).json()
    assert first["totals"]["to_run"] == second["totals"]["to_run"]


def test_plan_shows_the_command_that_will_run(client):
    d = client.post("/api/engine/plan", json={
        "channel": "zepto", "engine": "competitor", "workers": 4,
    }).json()
    cmd = d["commands"][0]["command"]
    assert "oneds_competitor.batch_flow" in cmd
    assert "--run-id" in cmd
    assert d["environment"]["database"]


def test_health_does_not_depend_on_the_database(client):
    """A health check that fails when the DB is slow takes the console down
    exactly when an operator most needs to watch a run."""
    assert client.get("/health").json() == {"status": "ok"}

"""The console is admin-only, and must say nothing useful to a stranger."""
from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore")

ENGINE_ROUTES = [
    ("get", "/api/engine/channels"),
    ("get", "/api/engine/runs"),
    ("get", "/api/engine/config"),
    ("get", "/api/engine/compare?before=a&after=b"),
    ("get", "/api/engine/gate-warnings"),
]


@pytest.fixture
def anon():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


@pytest.mark.parametrize("method,path", ENGINE_ROUTES)
def test_engine_routes_require_a_session(anon, method, path):
    assert getattr(anon, method)(path).status_code == 401


def test_post_routes_require_a_session(anon):
    assert anon.post("/api/engine/scope", json={"channel": "zepto"}).status_code == 401
    assert anon.post("/api/engine/runs", json={"channel": "zepto"}).status_code == 401
    assert anon.post("/api/engine/plan", json={"channel": "zepto"}).status_code == 401


def test_health_and_login_stay_open(anon):
    """A health check behind auth cannot report that auth is broken."""
    assert anon.get("/health").status_code == 200
    assert anon.get("/login").status_code == 200


def test_pages_redirect_when_signed_out(anon):
    """Serving the page and letting its JavaScript discover the 401 renders an
    empty shell first, which reads as a broken screen."""
    for path in ("/", "/configuration", "/history"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert "/login" in r.headers["location"]


def test_bad_password_is_rejected(anon, accounts):
    r = anon.post("/api/session/login",
                  json={"email": accounts["admin"]["email"], "password": "wrong"})
    assert r.status_code == 401


def test_unknown_user_is_indistinguishable_from_a_bad_password(anon, accounts):
    """Same status and same message, so the endpoint cannot be used to find
    out who has an account."""
    bad_pw = anon.post("/api/session/login",
                       json={"email": accounts["admin"]["email"], "password": "wrong"})
    unknown = anon.post("/api/session/login",
                        json={"email": "nobody@nowhere.invalid", "password": "x"})
    assert unknown.status_code == bad_pw.status_code == 401
    assert unknown.json()["detail"] == bad_pw.json()["detail"]


def test_user_role_is_refused(anon, accounts):
    """USER accounts have no read-only mode: the screen that shows a scope is
    the screen that launches it."""
    r = anon.post("/api/session/login", json=accounts["user"])
    assert r.status_code == 403


def test_admin_signs_in_and_reaches_the_engine(anon, accounts):
    r = anon.post("/api/session/login", json=accounts["admin"])
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "ADMIN"
    assert "console_session" in r.cookies

    assert anon.get("/api/session/me").status_code == 200
    assert anon.get("/api/engine/channels").status_code == 200


def test_logout_ends_the_session(anon, accounts):
    anon.post("/api/session/login", json=accounts["admin"])
    assert anon.post("/api/session/logout").status_code == 200
    anon.cookies.clear()
    assert anon.get("/api/engine/runs").status_code == 401


@pytest.mark.parametrize("overrides", [
    {"TOP_N_OUTPUT": "5"},          # locked: the portal renders exactly three
    {"NOT_A_SETTING": "1"},
    {"SEMANTIC_TOPK": "99999"},     # out of range
    {"W_PACK": "0.9"},              # weights must still sum to 1.00
])
def test_bad_overrides_are_refused_before_launch(client, overrides):
    """A bad weight does not crash the engine -- it quietly produces a
    different ranking, so it has to fail at the boundary."""
    r = client.post("/api/engine/runs", json={
        "channel": "zepto", "pipeline": "competitor",
        "category": "lip makeup", "subcategory": "lip balms",
        "workers": 1, "overrides": overrides,
    })
    assert r.status_code == 400

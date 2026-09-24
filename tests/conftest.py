"""Shared fixtures.

The auth tests need both an ADMIN and a USER account to prove the console
refuses the latter. They create their own and remove them afterwards rather
than depending on rows that happen to exist: config.users is a live, shared
table -- the portal reads it too -- so a test must never assume a particular
person is in it, and must never leave a working login behind.
"""
from __future__ import annotations

import os
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    """Refuse to run against a database nobody agreed to mutate.

    These tests write: they create and delete accounts, reject matches, reset
    rows and delete mapping rows. That was survivable while the DSN pointed at
    a dev copy. It is not survivable against the live database, and the DSN is
    a single line in .env that gets repointed when someone switches
    environments -- which is exactly what happened, and a test then wiped 174
    real rejections and 520 mapping rows.

    So the database has to be named. Set CONSOLE_TEST_DB to the database the
    suite is allowed to touch; if the DSN points anywhere else, the run stops
    before a single test executes.

        $env:CONSOLE_TEST_DB = "AureusSentinelv3_staging"

    A safety check that can be skipped by forgetting an env var would be no
    check at all, so the default is to refuse.
    """
    # .env has not been read yet at collection time -- app.py loads it on
    # import, which happens later. Read it here, or the refusal message names
    # an empty database and tells the reader nothing.
    try:
        from dotenv import load_dotenv
        load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    dsn = os.environ.get("SQL_SERVER_DSN", "")
    current = ""
    for part in dsn.split(";"):
        if part.strip().lower().startswith("database="):
            current = part.split("=", 1)[1].strip()
            break

    allowed = os.environ.get("CONSOLE_TEST_DB", "").strip()
    if not allowed:
        pytest.exit(
            f"\n\nRefusing to run: CONSOLE_TEST_DB is not set.\n"
            f"The DSN currently points at '{current}'. These tests WRITE -- they\n"
            f"reject matches, reset rows and delete mapping rows.\n\n"
            f"If '{current}' is a database you are willing to have mutated:\n"
            f'    $env:CONSOLE_TEST_DB = "{current}"\n',
            returncode=3,
        )
    if current.lower() != allowed.lower():
        pytest.exit(
            f"\n\nRefusing to run: the DSN points at '{current}' but\n"
            f"CONSOLE_TEST_DB says '{allowed}'. Nothing has been touched.\n",
            returncode=3,
        )

TEST_PASSWORD = "Fixture-Passw0rd!"
ADMIN_EMAIL = "pytest-console-admin@local.invalid"
USER_EMAIL = "pytest-console-user@local.invalid"


@pytest.fixture(scope="session")
def accounts():
    """An ADMIN and a USER account, removed when the session ends.

    The .invalid TLD is reserved by RFC 2606 and can never be a real address,
    so these cannot collide with a person's account or be mistaken for one.
    """
    from argon2 import PasswordHasher
    import sqlalchemy as sa
    from common import db

    conn = db.get_connection()
    hasher = PasswordHasher()
    digest = hasher.hash(TEST_PASSWORD)

    def remove():
        conn.execute(
            sa.text("DELETE FROM config.users WHERE email IN (:a, :u)"),
            {"a": ADMIN_EMAIL, "u": USER_EMAIL},
        )
        conn.commit()

    remove()  # in case an earlier run died before its teardown
    conn.execute(
        sa.text(
            """INSERT INTO config.users
                 (name, email, password_hash, role, source_ownership,
                  status, force_password_change, created_at, updated_at)
               VALUES
                 ('Pytest Console Admin', :a, :h, 'ADMIN', 'AllSources',
                  1, 0, SYSUTCDATETIME(), SYSUTCDATETIME()),
                 ('Pytest Console User',  :u, :h, 'USER',  'AllSources',
                  1, 0, SYSUTCDATETIME(), SYSUTCDATETIME())"""
        ),
        {"a": ADMIN_EMAIL, "u": USER_EMAIL, "h": digest},
    )
    conn.commit()

    yield {
        "admin": {"email": ADMIN_EMAIL, "password": TEST_PASSWORD},
        "user": {"email": USER_EMAIL, "password": TEST_PASSWORD},
    }

    remove()


@pytest.fixture
def client(accounts):
    """A TestClient already signed in as the fixture admin."""
    from fastapi.testclient import TestClient
    from app import app

    c = TestClient(app)
    c.post("/api/session/login", json=accounts["admin"])
    return c

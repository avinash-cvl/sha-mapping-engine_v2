"""Shared fixtures.

The auth tests need both an ADMIN and a USER account to prove the console
refuses the latter. They create their own and remove them afterwards rather
than depending on rows that happen to exist: config.users is a live, shared
table -- the portal reads it too -- so a test must never assume a particular
person is in it, and must never leave a working login behind.
"""
from __future__ import annotations

import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

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

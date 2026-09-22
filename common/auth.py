"""Authentication for the console, against the existing user store.

Deliberately reuses config.users -- 25 accounts, argon2id hashes, an ADMIN /
USER role column and source_ownership already in place -- rather than
standing up a parallel identity. A second user table is a second thing to
keep in sync, and the first time they disagree someone is locked out of the
portal but not the console, or worse, the reverse.

ADMIN accounts only. The console has no read-only mode because almost
nothing here is harmless to see and then act on: the same screen that shows
a scope launches it, and a run over amazon competitor is 151k SKUs and
roughly 453k LLM calls. Rather than split hairs over which endpoint is safe
for a USER, every route requires ADMIN -- currently 2 of the 25 accounts.

If a read-only tier is wanted later, the hook is require_admin: give it a
second dependency that allows USER on the GET routes.

What this does NOT do is re-implement the portal's own login flow or touch
its tokens. It issues its own short-lived JWT for the console session and
validates it on every request.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Depends, HTTPException, Request

from common import db, db_models

# A missing secret must fail loudly at import rather than silently signing
# every token with a known default -- that is the whole value of the token.
JWT_SECRET = os.environ.get("CONSOLE_JWT_SECRET")
JWT_ALG = "HS256"
TOKEN_TTL_HOURS = int(os.environ.get("CONSOLE_TOKEN_TTL_HOURS", "8"))
COOKIE = "console_session"

_hasher = PasswordHasher()


class User:
    def __init__(self, row):
        self.id = row.id
        self.email = row.email
        self.name = row.name
        self.role = (row.role or "USER").upper()

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    def as_dict(self) -> dict:
        return {"email": self.email, "name": self.name, "role": self.role}


def _secret() -> str:
    if not JWT_SECRET:
        raise HTTPException(
            500,
            "CONSOLE_JWT_SECRET is not set. The console refuses to sign tokens "
            "with a default secret.",
        )
    return JWT_SECRET


def authenticate(conn: db.Connection, email: str, password: str) -> User:
    """Verify a password against config.users.

    Failures are deliberately indistinguishable to the caller: an unknown
    address, a wrong password and a disabled account all return the same
    message, so the endpoint cannot be used to enumerate who has an account.
    """
    users = db_models.get_table(conn.engine, "config", "users")
    row = conn.execute(
        sa.select(users).where(sa.func.lower(users.c.email) == email.strip().lower())
    ).first()

    generic = HTTPException(401, "Email or password is incorrect.")
    if row is None:
        # Verify against a dummy hash anyway. Returning early here would make
        # an unknown address measurably faster to reject than a known one.
        try:
            _hasher.verify(
                "$argon2id$v=19$m=65536,t=3,p=4$"
                "c29tZXNhbHRzb21lc2FsdA$J3vX4Q0pXK8Z0qYyqvJ0Yw", "x"
            )
        except Exception:
            pass
        raise generic

    try:
        _hasher.verify(row.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        raise generic

    if hasattr(row, "status") and row.status is not None and not row.status:
        raise generic

    return User(row)


def issue_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user.email,
            "name": user.name,
            "role": user.role,
            "iat": now,
            "exp": now + timedelta(hours=TOKEN_TTL_HOURS),
        },
        _secret(),
        algorithm=JWT_ALG,
    )


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, _secret(), algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired. Sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session.")


def current_user(request: Request) -> User:
    """Resolve the caller. Accepts the session cookie the console sets, or a
    bearer token so the API stays usable from curl and scripts."""
    token = request.cookies.get(COOKIE)
    if not token:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    if not token:
        raise HTTPException(401, "Not signed in.")

    claims = _decode(token)

    class _Row:
        id = None
        email = claims.get("sub")
        name = claims.get("name")
        role = claims.get("role")

    return User(_Row())


def require_admin(user: User = Depends(current_user)) -> User:
    """Every console route depends on this.

    403 rather than 404: the caller is authenticated and the route exists,
    so pretending otherwise would just send them hunting for a typo.
    """
    if not user.is_admin:
        raise HTTPException(403, "The engine console requires an admin account.")
    return user

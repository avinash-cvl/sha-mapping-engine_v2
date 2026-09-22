"""Sign-in for the console.

Three endpoints and no user management: accounts live in config.users and are
administered by the portal that already owns them. This only exchanges a
password for a session.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from common import auth, db

router = APIRouter(prefix="/api/session", tags=["session"])

# Secure cookies require HTTPS, so they cannot be the default on a local
# http://127.0.0.1 dev server -- the browser would silently drop them and the
# console would appear broken. Opt in explicitly when deploying behind TLS.
SECURE_COOKIES = os.environ.get("CONSOLE_SECURE_COOKIES", "false").lower() == "true"


class Credentials(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(creds: Credentials, response: Response) -> dict:
    conn = db.get_connection()
    user = auth.authenticate(conn, creds.email, creds.password)

    # Checked after the password, not before: an inactive account must fail
    # the same way a wrong password does, or the response tells an attacker
    # the address is real.
    if not user.is_admin:
        from fastapi import HTTPException
        raise HTTPException(403, "The engine console requires an admin account.")

    token = auth.issue_token(user)
    response.set_cookie(
        auth.COOKIE, token,
        httponly=True,          # not readable from JS, so XSS cannot lift it
        samesite="lax",         # survives normal navigation, blocks cross-site POST
        secure=SECURE_COOKIES,
        max_age=auth.TOKEN_TTL_HOURS * 3600,
        path="/",
    )
    return {"user": user.as_dict(), "token": token}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: auth.User = Depends(auth.current_user)) -> dict:
    return {"user": user.as_dict()}

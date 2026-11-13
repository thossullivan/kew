"""Login/session for the inbox. Token in body only (never query).

There is no logout route: nothing in the inbox ever called one. A session
ends when its 12h TTL expires or when the server restarts, because the
session store is in-process (see `kew.serve.auth.SessionStore`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from kew.serve.auth import verify_token
from kew.serve.security import cookie_secure

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

_TTL_SECONDS = 12 * 60 * 60


class LoginBody(BaseModel):
    token: str


@auth_router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    if not verify_token(body.token, request.app.state.token):
        raise HTTPException(status_code=401, detail="invalid token")
    sid, csrf = request.app.state.sessions.create()
    secure = cookie_secure(request)
    response.set_cookie(
        "kew_session",
        sid,
        max_age=_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        "kew_csrf",
        csrf,
        max_age=_TTL_SECONDS,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return {"csrf_token": csrf}


@auth_router.get("/session")
def session(request: Request) -> dict[str, Any]:
    s = request.app.state.sessions.get(request.cookies.get("kew_session"))
    if s is None:
        return {"authenticated": False}
    return {"authenticated": True, "csrf_token": s.csrf_token}

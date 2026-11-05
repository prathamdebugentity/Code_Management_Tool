from __future__ import annotations

import asyncio
import hmac
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import get_settings
from app.dependencies import SESSION_COOKIE, current_session, get_aws_service, get_session_store, templates
from app.services.github_service import GitHubError, GitHubService

router = APIRouter(tags=["auth"])


def _login_page(request: Request, error: str | None = None, username: str = "", status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request, "login.html",
        {"error": error, "username": username, "dry_run": get_aws_service().dry_run},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if current_session(request):
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@router.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form(max_length=100)],
    password: Annotated[str, Form(max_length=200)],
    github_token: Annotated[str, Form(max_length=255)] = "",
) -> Response:
    settings = get_settings()
    # Evaluate both comparisons to keep timing independent of which field is wrong.
    user_ok = hmac.compare_digest(username.encode(), settings.app_username.encode())
    pass_ok = hmac.compare_digest(password.encode(), settings.app_password.encode())
    if not (user_ok and pass_ok):
        await asyncio.sleep(1)
        return _login_page(request, "Invalid username or password.", username, 401)

    token = github_token.strip() or None
    github_login = None
    if token:
        try:
            github_login = (await GitHubService(token, settings.github_api_url).validate_token())["login"]
        except GitHubError as exc:
            return _login_page(request, f"GitHub token rejected: {exc.message}", username, 400)

    session = get_session_store().create(username)
    session.github_token = token
    session.github_login = github_login

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, session.sid, max_age=settings.session_ttl_seconds,
        httponly=True, samesite="lax", secure=settings.cookie_secure,
    )
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: Annotated[str, Form()] = "") -> Response:
    session = current_session(request)
    if session and hmac.compare_digest(csrf_token.encode(), session.csrf_token.encode()):
        get_session_store().delete(session.sid)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response

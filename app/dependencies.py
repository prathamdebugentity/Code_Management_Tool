from __future__ import annotations

import hmac
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, get_settings
from app.services.aws_service import AwsService
from app.services.deployment_manager import DeploymentManager
from app.services.github_service import GitHubService
from app.services.session_store import SessionStore, UserSession

SESSION_COOKIE = "cmt_session"

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@lru_cache
def get_session_store() -> SessionStore:
    return SessionStore(get_settings().session_ttl_seconds)


@lru_cache
def get_aws_service() -> AwsService:
    return AwsService(get_settings())


@lru_cache
def get_deployment_manager() -> DeploymentManager:
    return DeploymentManager(get_aws_service(), get_settings())


def current_session(request: Request) -> UserSession | None:
    sid = request.cookies.get(SESSION_COOKIE)
    return get_session_store().get(sid) if sid else None


def require_session(request: Request) -> UserSession:
    session = current_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return session


def require_csrf(request: Request, session: Annotated[UserSession, Depends(require_session)]) -> UserSession:
    token = request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(token.encode(), session.csrf_token.encode()):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")
    return session


def github_client(session: UserSession) -> GitHubService:
    settings = get_settings()
    if session.github_token:
        return GitHubService(session.github_token, settings.github_api_url)
    if get_aws_service().dry_run:
        return GitHubService(None, settings.github_api_url, mock=True)
    raise HTTPException(status_code=400, detail="Connect a GitHub Personal Access Token first.")

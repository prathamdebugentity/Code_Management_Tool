from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import get_settings
from app.dependencies import current_session, get_aws_service, templates
from app.services.github_service import DEFAULT_PROCFILE_COMMAND, sample_eb_files

router = APIRouter(tags=["ui"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    session = current_session(request)
    if session is None:
        return RedirectResponse("/login", status_code=303)
    aws = get_aws_service()
    response = templates.TemplateResponse(
        request, "index.html",
        {
            "user": session.username,
            "csrf_token": session.csrf_token,
            "dry_run": aws.dry_run,
            "github_login": session.github_login,
            "github_mock": session.github_token is None and aws.dry_run,
            "accounts": aws.list_accounts(),
            "max_upload_mb": get_settings().max_upload_mb,
            "sample_files": list(sample_eb_files()),
            "default_procfile": DEFAULT_PROCFILE_COMMAND,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok", "mode": "dry-run" if get_aws_service().dry_run else "live"}

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, get_settings
from app.dependencies import get_aws_service
from app.routers import auth, aws, github, ui
from app.services.aws_service import AwsError
from app.services.bundle import BundleError
from app.services.github_service import GitHubError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cmt")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    aws_service = get_aws_service()
    logger.info("CMT Deployer starting in %s mode; accounts: %s",
                "DRY-RUN" if aws_service.dry_run else "LIVE", ", ".join(settings.accounts))
    if settings.using_default_credentials:
        logger.warning("Using default admin/admin login. Set APP_USERNAME and APP_PASSWORD.")
    yield


app = FastAPI(title="CMT Deployer", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(auth.router)
app.include_router(ui.router)
app.include_router(github.router)
app.include_router(aws.router)


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(GitHubError)
@app.exception_handler(AwsError)
@app.exception_handler(BundleError)
async def service_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=getattr(exc, "status_code", 500), content={"detail": getattr(exc, "message", str(exc))})

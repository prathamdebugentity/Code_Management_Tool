from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.dependencies import get_aws_service, get_deployment_manager, github_client, require_csrf, require_session
from app.services.bundle import BundleError, inspect_bundle, repack_github_archive
from app.services.deployment_manager import Deployment
from app.services.github_service import validate_branch, validate_repo
from app.services.session_store import UserSession

router = APIRouter(prefix="/api/aws", tags=["aws"])

_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,38}[A-Za-z0-9]$")
SessionDep = Annotated[UserSession, Depends(require_session)]
CsrfSessionDep = Annotated[UserSession, Depends(require_csrf)]


@router.get("/accounts")
async def list_accounts(_: SessionDep) -> dict[str, Any]:
    aws = get_aws_service()
    return {"dry_run": aws.dry_run, "accounts": aws.list_accounts()}


@router.get("/accounts/{account_key}/environments")
async def list_environments(account_key: str, _: SessionDep) -> list[dict[str, Any]]:
    return await asyncio.to_thread(get_aws_service().list_environments, account_key)


@router.get("/accounts/{account_key}/identity")
async def caller_identity(account_key: str, _: SessionDep) -> dict[str, str]:
    return await asyncio.to_thread(get_aws_service().caller_identity, account_key)


async def _save_upload(upload: UploadFile, max_bytes: int) -> tuple[Path, int]:
    if not upload.filename or not upload.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Bundle must be a .zip file.")
    fd, tmp_name = tempfile.mkstemp(prefix="cmt-upload-", suffix=".zip")
    tmp = Path(tmp_name)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"Bundle exceeds {max_bytes // 1_048_576} MB.")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, size


@router.post("/deploy", status_code=202)
async def deploy(
    session: CsrfSessionDep,
    account: Annotated[str, Form(max_length=50)],
    environment: Annotated[str, Form(max_length=40)],
    source: Annotated[Literal["upload", "github"], Form()],
    bundle: Annotated[UploadFile | None, File()] = None,
    repo: Annotated[str | None, Form(max_length=200)] = None,
    ref: Annotated[str | None, Form(max_length=200)] = None,
) -> dict[str, Any]:
    settings = get_settings()
    aws = get_aws_service()
    manager = get_deployment_manager()

    aws.account(account)
    if not _ENV_NAME_RE.fullmatch(environment):
        raise HTTPException(status_code=400, detail="Invalid environment name.")
    active = manager.active_for(account, environment)
    if active:
        raise HTTPException(status_code=409, detail=f"Deployment {active.id[:8]} is already running for this environment.")

    cleanup: list[Path] = []
    if source == "upload":
        if bundle is None:
            raise HTTPException(status_code=400, detail="Select a .zip bundle to upload.")
        tmp, size = await _save_upload(bundle, settings.max_upload_bytes)
        cleanup.append(tmp)
        try:
            warnings = await asyncio.to_thread(inspect_bundle, tmp, settings.max_uncompressed_bytes)
        except BundleError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        filename = bundle.filename

        async def provider(dep: Deployment, dest: Path) -> None:
            await asyncio.to_thread(shutil.move, str(tmp), str(dest))
            dep.log(f"Using uploaded bundle '{filename}' ({size / 1_048_576:.2f} MB).")
            for warning in warnings:
                dep.log(warning, "warn")
        description = f"upload:{filename}"
    else:
        try:
            repo_name = validate_repo(repo or "")
            ref_name = validate_branch(ref or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        gh = github_client(session)

        async def provider(dep: Deployment, dest: Path) -> None:
            archive = dest.with_name("source.zip")
            dep.log(f"Downloading {repo_name}@{ref_name} from GitHub{' (simulated)' if gh.is_mock else ''}...")
            size = await gh.download_archive(repo_name, ref_name, archive, settings.max_upload_bytes)
            dep.log(f"Downloaded archive ({size / 1_048_576:.2f} MB); repackaging for Elastic Beanstalk...")
            count = await asyncio.to_thread(repack_github_archive, archive, dest, settings.max_uncompressed_bytes)
            archive.unlink(missing_ok=True)
            dep.log(f"Repackaged {count} files.")
            for warning in await asyncio.to_thread(inspect_bundle, dest, settings.max_uncompressed_bytes):
                dep.log(warning, "warn")
        description = f"github:{repo_name}@{ref_name}"

    dep = manager.start(owner=session.username, account_key=account, environment=environment,
                        source=description, provider=provider, cleanup=cleanup)
    return {"deployment_id": dep.id, "events_url": f"/api/aws/deployments/{dep.id}/events"}


def _get_owned(deployment_id: str, session: UserSession) -> Deployment:
    dep = get_deployment_manager().get(deployment_id, session.username)
    if dep is None:
        raise HTTPException(status_code=404, detail="Deployment not found.")
    return dep


@router.get("/deployments/{deployment_id}")
async def deployment_status(deployment_id: str, session: SessionDep) -> dict[str, Any]:
    return _get_owned(deployment_id, session).summary()


@router.get("/deployments/{deployment_id}/events")
async def deployment_events(deployment_id: str, request: Request, session: SessionDep) -> StreamingResponse:
    dep = _get_owned(deployment_id, session)
    try:
        cursor = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        cursor = 0

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        last_beat = time.monotonic()
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            while cursor < len(dep.logs):
                entry = dep.logs[cursor]
                yield f"id: {entry['id']}\nevent: log\ndata: {json.dumps(entry)}\n\n"
                cursor += 1
                last_beat = time.monotonic()
            if dep.finished and cursor >= len(dep.logs):
                yield f"event: done\ndata: {json.dumps(dep.summary())}\n\n"
                return
            if time.monotonic() - last_beat > 15:
                yield ": keep-alive\n\n"
                last_beat = time.monotonic()
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

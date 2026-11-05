from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field

from app.config import get_settings
from app.dependencies import get_aws_service, github_client, require_csrf, require_session
from app.services.github_service import (
    DEFAULT_PROCFILE_COMMAND,
    GitHubService,
    sample_eb_files,
    validate_branch,
    validate_repo,
)
from app.services.session_store import UserSession

router = APIRouter(prefix="/api/github", tags=["github"])

RepoName = Annotated[str, AfterValidator(validate_repo)]
BranchName = Annotated[str, AfterValidator(validate_branch)]
SessionDep = Annotated[UserSession, Depends(require_session)]
CsrfSessionDep = Annotated[UserSession, Depends(require_csrf)]


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=255)


class CreateBranchIn(BaseModel):
    repo: RepoName
    base_branch: BranchName
    new_branch: BranchName | None = None


class CommitConfigIn(BaseModel):
    repo: RepoName
    branch: BranchName
    procfile_command: str = Field(default=DEFAULT_PROCFILE_COMMAND, pattern=r"^[^\r\n]{1,300}$")
    files: list[str] = Field(min_length=1)
    message: str = Field(default="Add Elastic Beanstalk deployment configuration", max_length=200)


def default_deploy_branch() -> str:
    return f"deploy/eb-update-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"


@router.get("/status")
async def status(session: SessionDep) -> dict[str, Any]:
    return {
        "connected": session.github_token is not None,
        "login": session.github_login,
        "mock": session.github_token is None and get_aws_service().dry_run,
    }


@router.post("/token")
async def save_token(body: TokenIn, session: CsrfSessionDep) -> dict[str, Any]:
    token = body.token.strip()
    info = await GitHubService(token, get_settings().github_api_url).validate_token()
    session.github_token = token
    session.github_login = info["login"]
    return {"connected": True, "login": info["login"], "scopes": info["scopes"]}


@router.delete("/token")
async def clear_token(session: CsrfSessionDep) -> dict[str, bool]:
    session.github_token = None
    session.github_login = None
    return {"connected": False}


@router.get("/repos")
async def list_repos(session: SessionDep) -> list[dict[str, Any]]:
    return await github_client(session).list_repos()


@router.get("/branches")
async def list_branches(session: SessionDep, repo: Annotated[str, Query(max_length=200)]) -> dict[str, Any]:
    try:
        repo = validate_repo(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    gh = github_client(session)
    info = await gh.get_repo(repo)
    return {"repo": info, "branches": await gh.list_branches(repo), "suggested_branch": default_deploy_branch()}


@router.post("/branches")
async def create_branch(body: CreateBranchIn, session: CsrfSessionDep) -> dict[str, str]:
    new_branch = body.new_branch or default_deploy_branch()
    return await github_client(session).create_branch(body.repo, body.base_branch, new_branch)


@router.get("/sample-config")
async def sample_config(_: SessionDep, procfile_command: str = DEFAULT_PROCFILE_COMMAND) -> dict[str, str]:
    return sample_eb_files(procfile_command.replace("\r", " ").replace("\n", " ")[:300])


@router.post("/commit-config")
async def commit_config(body: CommitConfigIn, session: CsrfSessionDep) -> dict[str, Any]:
    available = sample_eb_files(body.procfile_command)
    unknown = set(body.files) - available.keys()
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown config files: {', '.join(sorted(unknown))}")
    files = {path: available[path] for path in body.files}
    result = await github_client(session).commit_files(body.repo, body.branch, files, body.message)
    return {**result, "files": list(files)}

from __future__ import annotations

import hashlib
import io
import re
import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

DEFAULT_PROCFILE_COMMAND = "gunicorn --bind :8000 --workers 3 --threads 2 application:application"


class GitHubError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_repo(name: str) -> str:
    name = name.strip()
    if not _REPO_RE.fullmatch(name) or name.split("/", 1)[1] in {".", ".."}:
        raise ValueError("Repository must be in 'owner/repo' format.")
    return name


def validate_branch(name: str) -> str:
    name = name.strip()
    if (
        not _BRANCH_RE.fullmatch(name)
        or name.startswith(("/", "-", "."))
        or name.endswith(("/", ".", ".lock"))
        or ".." in name
        or "//" in name
        or "/." in name
    ):
        raise ValueError(f"Invalid branch name: {name!r}")
    return name


def sample_eb_files(procfile_command: str = DEFAULT_PROCFILE_COMMAND) -> dict[str, str]:
    return {
        "Procfile": f"web: {procfile_command}\n",
        ".ebextensions/01-environment.config": (
            "option_settings:\n"
            "  aws:elasticbeanstalk:application:environment:\n"
            "    APP_ENV: production\n"
            '    PYTHONUNBUFFERED: "1"\n'
            "  aws:elasticbeanstalk:environment:proxy:staticfiles:\n"
            "    /static: static\n"
        ),
        ".platform/nginx/conf.d/upload_size.conf": "client_max_body_size 50M;\n",
    }


def _mock_archive(repo: str, ref: str) -> bytes:
    prefix = f"{repo.replace('/', '-')}-{hashlib.sha1(ref.encode()).hexdigest()[:7]}/"
    files = {
        "application.py": (
            "def application(environ, start_response):\n"
            '    start_response("200 OK", [("Content-Type", "text/plain")])\n'
            '    return [b"Hello from the CMT sample app\\n"]\n'
        ),
        "requirements.txt": "gunicorn>=22.0\n",
        **sample_eb_files(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(prefix + path, content)
    return buffer.getvalue()


class _MockGitHub:
    """In-memory stand-in used in dry-run mode when no PAT is stored."""

    _lock = threading.Lock()
    _branches: dict[str, dict[str, str]] = {}

    def _repo(self, repo: str) -> dict[str, str]:
        with self._lock:
            return self._branches.setdefault(
                repo,
                {"main": hashlib.sha1(f"{repo}:main".encode()).hexdigest(),
                 "develop": hashlib.sha1(f"{repo}:develop".encode()).hexdigest()},
            )

    def validate_token(self) -> dict[str, Any]:
        return {"login": "dry-run-user", "name": "Dry Run", "scopes": "repo (simulated)"}

    def list_repos(self) -> list[dict[str, Any]]:
        return [
            {"full_name": "demo-org/sample-flask-app", "default_branch": "main", "private": True},
            {"full_name": "demo-org/payments-api", "default_branch": "main", "private": True},
        ]

    def list_branches(self, repo: str) -> list[str]:
        return sorted(self._repo(repo))

    def create_branch(self, repo: str, base: str, new: str) -> dict[str, str]:
        branches = self._repo(repo)
        with self._lock:
            if base not in branches:
                raise GitHubError(f"Base branch '{base}' not found.", 404)
            if new in branches:
                raise GitHubError(f"Branch '{new}' already exists.", 409)
            branches[new] = branches[base]
        return {"name": new, "sha": branches[new]}

    def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> dict[str, str]:
        branches = self._repo(repo)
        with self._lock:
            if branch not in branches:
                raise GitHubError(f"Branch '{branch}' not found.", 404)
            sha = hashlib.sha1((branches[branch] + message + "".join(sorted(files))).encode()).hexdigest()
            branches[branch] = sha
        return {"sha": sha, "html_url": f"https://github.com/{repo}/commit/{sha}"}


class GitHubService:
    def __init__(self, token: str | None, api_url: str = "https://api.github.com", mock: bool = False) -> None:
        if not token and not mock:
            raise GitHubError("A GitHub Personal Access Token is required.", 400)
        self._token = token
        self._api_url = api_url
        self._mock = _MockGitHub() if mock else None

    @property
    def is_mock(self) -> bool:
        return self._mock is not None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cmt-deployer",
        }

    @staticmethod
    def _error(resp: httpx.Response) -> GitHubError:
        try:
            detail = resp.json().get("message", resp.text)
        except ValueError:
            detail = resp.text or resp.reason_phrase
        if resp.status_code == 401:
            return GitHubError("GitHub token is invalid or expired.", 400)
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            return GitHubError("GitHub API rate limit exceeded; try again later.", 429)
        if resp.status_code == 403:
            return GitHubError(f"GitHub denied the request (check token scopes): {detail}", 403)
        if resp.status_code == 404:
            return GitHubError("Not found on GitHub, or the token has no access to it.", 404)
        if resp.status_code in (409, 422):
            return GitHubError(f"GitHub rejected the request: {detail}", 409)
        return GitHubError(f"GitHub API error {resp.status_code}: {detail}", 502)

    async def _request(self, method: str, path: str, expected: tuple[int, ...] = (200,), **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(base_url=self._api_url, headers=self._headers(), timeout=_TIMEOUT) as client:
                resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub: {exc}") from exc
        if resp.status_code not in expected:
            raise self._error(resp)
        return resp

    async def validate_token(self) -> dict[str, Any]:
        if self._mock:
            return self._mock.validate_token()
        resp = await self._request("GET", "/user")
        data = resp.json()
        return {"login": data["login"], "name": data.get("name"), "scopes": resp.headers.get("x-oauth-scopes", "fine-grained")}

    async def list_repos(self) -> list[dict[str, Any]]:
        if self._mock:
            return self._mock.list_repos()
        resp = await self._request(
            "GET", "/user/repos",
            params={"per_page": 100, "sort": "updated", "affiliation": "owner,collaborator,organization_member"},
        )
        return [
            {"full_name": r["full_name"], "default_branch": r.get("default_branch"), "private": r.get("private", False)}
            for r in resp.json()
        ]

    async def get_repo(self, repo: str) -> dict[str, Any]:
        repo = validate_repo(repo)
        if self._mock:
            return {"full_name": repo, "default_branch": "main", "private": True, "html_url": f"https://github.com/{repo}"}
        data = (await self._request("GET", f"/repos/{repo}")).json()
        return {"full_name": data["full_name"], "default_branch": data["default_branch"],
                "private": data.get("private", False), "html_url": data.get("html_url")}

    async def list_branches(self, repo: str) -> list[str]:
        repo = validate_repo(repo)
        if self._mock:
            return self._mock.list_branches(repo)
        names: list[str] = []
        for page in range(1, 6):
            batch = (await self._request("GET", f"/repos/{repo}/branches", params={"per_page": 100, "page": page})).json()
            names.extend(b["name"] for b in batch)
            if len(batch) < 100:
                break
        return names

    async def _branch_sha(self, repo: str, branch: str) -> str:
        resp = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return resp.json()["object"]["sha"]

    async def create_branch(self, repo: str, base: str, new: str) -> dict[str, str]:
        repo, base, new = validate_repo(repo), validate_branch(base), validate_branch(new)
        if self._mock:
            return self._mock.create_branch(repo, base, new)
        sha = await self._branch_sha(repo, base)
        await self._request("POST", f"/repos/{repo}/git/refs", expected=(201,), json={"ref": f"refs/heads/{new}", "sha": sha})
        return {"name": new, "sha": sha}

    async def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> dict[str, str]:
        """Commit all files as a single commit via the Git Data API (blob -> tree -> commit -> ref)."""
        repo, branch = validate_repo(repo), validate_branch(branch)
        if not files:
            raise GitHubError("No files selected to commit.", 400)
        if self._mock:
            return self._mock.commit_files(repo, branch, files, message)
        parent_sha = await self._branch_sha(repo, branch)
        parent = (await self._request("GET", f"/repos/{repo}/git/commits/{parent_sha}")).json()
        tree = (await self._request(
            "POST", f"/repos/{repo}/git/trees", expected=(201,),
            json={
                "base_tree": parent["tree"]["sha"],
                "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()],
            },
        )).json()
        commit = (await self._request(
            "POST", f"/repos/{repo}/git/commits", expected=(201,),
            json={"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )).json()
        await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit["sha"]})
        return {"sha": commit["sha"], "html_url": commit.get("html_url", f"https://github.com/{repo}/commit/{commit['sha']}")}

    async def download_archive(self, repo: str, ref: str, dest: Path, max_bytes: int) -> int:
        repo, ref = validate_repo(repo), validate_branch(ref)
        if self._mock:
            data = _mock_archive(repo, ref)
            dest.write_bytes(data)
            return len(data)
        total = 0
        try:
            # httpx drops the Authorization header when GitHub redirects to codeload (cross-origin).
            async with httpx.AsyncClient(base_url=self._api_url, headers=self._headers(),
                                         timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True) as client:
                async with client.stream("GET", f"/repos/{repo}/zipball/{ref}") as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        raise self._error(resp)
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise GitHubError("Repository archive exceeds the maximum bundle size.", 413)
                            fh.write(chunk)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Archive download failed: {exc}") from exc
        return total

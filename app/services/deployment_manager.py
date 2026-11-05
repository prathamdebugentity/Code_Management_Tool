from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.services.aws_service import AwsError, AwsService
from app.services.bundle import BundleError
from app.services.github_service import GitHubError

logger = logging.getLogger(__name__)

ACTIVE_STATES = {"queued", "running"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeploymentFailed(Exception):
    pass


@dataclass
class Deployment:
    id: str
    owner: str
    account_key: str
    environment: str
    source: str
    created_at: datetime = field(default_factory=_utcnow)
    status: str = "queued"
    application: str | None = None
    version_label: str | None = None
    finished: bool = False
    logs: list[dict[str, Any]] = field(default_factory=list)

    def log(self, message: str, level: str = "info", source: str = "cmt") -> None:
        self.logs.append({"id": len(self.logs), "time": _utcnow().isoformat(),
                          "level": level, "source": source, "message": message})

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "account": self.account_key, "environment": self.environment,
            "application": self.application, "version_label": self.version_label, "source": self.source,
            "created_at": self.created_at.isoformat(), "finished": self.finished,
        }


BundleProvider = Callable[[Deployment, Path], Awaitable[None]]


class DeploymentManager:
    def __init__(self, aws: AwsService, settings: Settings) -> None:
        self.aws = aws
        self.settings = settings
        self._deployments: dict[str, Deployment] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._poll = min(settings.poll_interval_seconds, 1.0) if aws.dry_run else settings.poll_interval_seconds

    def get(self, deployment_id: str, owner: str) -> Deployment | None:
        dep = self._deployments.get(deployment_id)
        return dep if dep and dep.owner == owner else None

    def active_for(self, account_key: str, environment: str) -> Deployment | None:
        return next((d for d in self._deployments.values()
                     if d.account_key == account_key and d.environment == environment and d.status in ACTIVE_STATES), None)

    def start(self, *, owner: str, account_key: str, environment: str, source: str,
              provider: BundleProvider, cleanup: list[Path] | None = None) -> Deployment:
        dep = Deployment(id=uuid.uuid4().hex, owner=owner, account_key=account_key,
                         environment=environment, source=source)
        self._deployments[dep.id] = dep
        dep.log(f"Deployment {dep.id[:8]} queued by {owner} ({'DRY-RUN' if self.aws.dry_run else 'LIVE'} mode).")
        task = asyncio.create_task(self._run(dep, provider, cleanup or []))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return dep

    async def _run(self, dep: Deployment, provider: BundleProvider, cleanup: list[Path]) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="cmt-deploy-"))
        try:
            dep.status = "running"
            env = await asyncio.to_thread(self.aws.get_environment, dep.account_key, dep.environment)
            dep.application = env["application"]
            dep.log(f"Target {env['name']} (app {env['application']}) in account '{dep.account_key}': "
                    f"status {env['status']}, health {env['health']}, version {env['version_label']}.")
            if env["status"] != "Ready":
                raise DeploymentFailed(f"Environment is '{env['status']}'. Wait until it is Ready and retry.")

            bundle = workdir / "bundle.zip"
            dep.log("Preparing source bundle...")
            await provider(dep, bundle)
            dep.log(f"Bundle ready ({bundle.stat().st_size / 1_048_576:.2f} MB).")

            label = f"cmt-{_utcnow():%Y%m%d-%H%M%S}-{dep.id[:6]}"
            dep.version_label = label
            dep.log("Uploading bundle to S3...")
            bucket, key = await asyncio.to_thread(self.aws.upload_bundle, dep.account_key, dep.application, bundle, label)
            dep.log(f"Uploaded to s3://{bucket}/{key}")

            await asyncio.to_thread(self.aws.create_application_version, dep.account_key, dep.application,
                                    label, bucket, key, f"Deployed by {dep.owner} via CMT ({dep.source})")
            dep.log(f"Created application version '{label}'; waiting for Elastic Beanstalk to validate it...")
            await self._wait_for_version(dep)

            since = _utcnow() - timedelta(seconds=2)
            await asyncio.to_thread(self.aws.update_environment, dep.account_key, dep.environment, label)
            dep.log(f"update_environment triggered for {dep.environment}. Streaming Elastic Beanstalk events...")
            await self._watch(dep, since)
        except DeploymentFailed as exc:
            dep.status = "failed"
            dep.log(str(exc), "error")
        except (AwsError, GitHubError, BundleError) as exc:
            dep.status = "failed"
            dep.log(exc.message, "error")
        except Exception as exc:
            logger.exception("Deployment %s crashed", dep.id)
            dep.status = "failed"
            dep.log(f"Unexpected error: {exc}", "error")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            for path in cleanup:
                path.unlink(missing_ok=True)
            dep.log(f"Deployment finished with status: {dep.status.upper()}",
                    "success" if dep.status == "succeeded" else "error")
            dep.finished = True

    async def _wait_for_version(self, dep: Deployment) -> None:
        assert dep.application and dep.version_label
        deadline = asyncio.get_running_loop().time() + 600
        last = None
        while asyncio.get_running_loop().time() < deadline:
            status = await asyncio.to_thread(self.aws.application_version_status,
                                             dep.account_key, dep.application, dep.version_label)
            if status != last:
                dep.log(f"Application version status: {status}")
                last = status
            if status == "PROCESSED":
                return
            if status == "FAILED":
                raise DeploymentFailed("Elastic Beanstalk rejected the bundle (version status FAILED). "
                                       "Check the zip layout and platform configuration files.")
            await asyncio.sleep(self._poll)
        raise DeploymentFailed("Timed out waiting for the application version to be processed.")

    async def _watch(self, dep: Deployment, since: datetime) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.settings.deploy_timeout_seconds
        seen: set[tuple[str, str]] = set()
        errors = 0
        update_observed = False

        while loop.time() < deadline:
            for ev in await asyncio.to_thread(self.aws.describe_events, dep.account_key, dep.environment, since):
                key = (ev["time"], ev["message"])
                if key in seen:
                    continue
                seen.add(key)
                sev = ev["severity"].upper()
                level = "error" if sev in {"ERROR", "FATAL"} else "warn" if sev == "WARN" else "info"
                errors += level == "error"
                dep.log(f"[{sev}] {ev['message']}", level, source="eb")

            env = await asyncio.to_thread(self.aws.get_environment, dep.account_key, dep.environment)
            if env["status"] != "Ready":
                update_observed = True
            # EB can report the old "Ready" state briefly after update_environment returns.
            elif update_observed or errors or loop.time() - started > 60:
                dep.log(f"Environment is Ready: health {env['health']}"
                        f"{' (' + env['health_status'] + ')' if env.get('health_status') else ''}, "
                        f"version {env['version_label']}.", source="eb")
                if env["version_label"] != dep.version_label:
                    raise DeploymentFailed("Environment returned to Ready without the new version (deployment rolled back).")
                if errors or env["health"] == "Red":
                    raise DeploymentFailed("New version is live but Elastic Beanstalk reported errors; check environment health.")
                dep.status = "succeeded"
                return
            await asyncio.sleep(self._poll)
        raise DeploymentFailed("Timed out waiting for the environment update to finish.")

from __future__ import annotations

import functools
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import AwsAccount, Settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])
_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"}, user_agent_extra="cmt-deployer")
_ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                  "InvalidClientTokenId", "ExpiredToken", "InsufficientPrivilegesException"}


class AwsError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _wrap_errors(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except AwsError:
            raise
        except ClientError as exc:
            err = exc.response.get("Error", {})
            code = err.get("Code", "ClientError")
            raise AwsError(f"{code}: {err.get('Message', str(exc))}", 403 if code in _ACCESS_DENIED else 502) from exc
        except BotoCoreError as exc:
            raise AwsError(f"AWS SDK error: {exc}") from exc
    return wrapper  # type: ignore[return-value]


def _env_to_dict(env: dict[str, Any]) -> dict[str, Any]:
    updated = env.get("DateUpdated")
    return {
        "name": env["EnvironmentName"],
        "application": env["ApplicationName"],
        "status": env.get("Status", "Unknown"),
        "health": env.get("Health", "Grey"),
        "health_status": env.get("HealthStatus"),
        "version_label": env.get("VersionLabel"),
        "cname": env.get("CNAME"),
        "platform": env.get("SolutionStackName") or env.get("PlatformArn"),
        "tier": (env.get("Tier") or {}).get("Name"),
        "updated": updated.isoformat() if updated else None,
    }


class AwsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, tuple[boto3.Session, datetime | None]] = {}
        self._lock = threading.Lock()
        self.dry_run = self._resolve_dry_run()
        self._mock = MockBeanstalk(settings.accounts) if self.dry_run else None

    def _resolve_dry_run(self) -> bool:
        if self.settings.dry_run_mode != "auto":
            return self.settings.dry_run_mode == "true"
        try:
            return boto3.Session().get_credentials() is None
        except BotoCoreError:
            return True

    # ---- accounts / STS -------------------------------------------------

    def account(self, key: str) -> AwsAccount:
        acct = self.settings.accounts.get(key)
        if acct is None:
            raise AwsError(f"Unknown AWS account '{key}'.", 404)
        return acct

    def list_accounts(self) -> list[dict[str, Any]]:
        return [
            {"key": a.key, "name": a.name, "region": a.region, "account_id": a.account_id, "assume_role": bool(a.role_arn)}
            for a in self.settings.accounts.values()
        ]

    def assume_role_session(self, account: AwsAccount) -> tuple[boto3.Session, datetime | None]:
        """Return a boto3 session for the account, assuming its cross-account role when configured."""
        base = boto3.Session(region_name=account.region)
        if not account.role_arn:
            return base, None
        params: dict[str, Any] = {
            "RoleArn": account.role_arn,
            "RoleSessionName": "cmt-deployer",
            "DurationSeconds": 3600,
        }
        if account.external_id:
            params["ExternalId"] = account.external_id
        creds = base.client("sts", config=_BOTO_CONFIG).assume_role(**params)["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=account.region,
        )
        return session, creds["Expiration"]

    def _session(self, account: AwsAccount) -> boto3.Session:
        with self._lock:
            cached = self._sessions.get(account.key)
            if cached and (cached[1] is None or cached[1] - _utcnow() > timedelta(minutes=5)):
                return cached[0]
        session, expiration = self.assume_role_session(account)
        with self._lock:
            self._sessions[account.key] = (session, expiration)
        return session

    def _client(self, account_key: str, service: str) -> Any:
        return self._session(self.account(account_key)).client(service, config=_BOTO_CONFIG)

    @_wrap_errors
    def caller_identity(self, account_key: str) -> dict[str, str]:
        if self._mock:
            return {"Account": self.account(account_key).account_id or "000000000000", "Arn": "arn:aws:sts::dry-run"}
        ident = self._client(account_key, "sts").get_caller_identity()
        return {"Account": ident["Account"], "Arn": ident["Arn"]}

    # ---- Elastic Beanstalk ------------------------------------------------

    @_wrap_errors
    def list_environments(self, account_key: str) -> list[dict[str, Any]]:
        self.account(account_key)
        if self._mock:
            return self._mock.list_environments(account_key)
        eb = self._client(account_key, "elasticbeanstalk")
        envs: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"IncludeDeleted": False}
            if token:
                kwargs["NextToken"] = token
            resp = eb.describe_environments(**kwargs)
            envs.extend(resp.get("Environments", []))
            token = resp.get("NextToken")
            if not token:
                break
        return sorted((_env_to_dict(e) for e in envs if e.get("Status") != "Terminated"), key=lambda e: e["name"])

    @_wrap_errors
    def get_environment(self, account_key: str, env_name: str) -> dict[str, Any]:
        self.account(account_key)
        if self._mock:
            return self._mock.get_environment(account_key, env_name)
        envs = self._client(account_key, "elasticbeanstalk").describe_environments(
            EnvironmentNames=[env_name], IncludeDeleted=False
        ).get("Environments", [])
        if not envs:
            raise AwsError(f"Environment '{env_name}' not found.", 404)
        return _env_to_dict(envs[0])

    @_wrap_errors
    def upload_bundle(self, account_key: str, application: str, path: Path, label: str) -> tuple[str, str]:
        key = f"cmt-deployments/{application}/{label}.zip"
        if self._mock:
            return self._mock.upload_bundle(path, key)
        session = self._session(self.account(account_key))
        bucket = session.client("elasticbeanstalk", config=_BOTO_CONFIG).create_storage_location()["S3Bucket"]
        session.client("s3", config=_BOTO_CONFIG).upload_file(
            str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"}
        )
        return bucket, key

    @_wrap_errors
    def create_application_version(self, account_key: str, application: str, label: str,
                                   bucket: str, key: str, description: str) -> None:
        if self._mock:
            return self._mock.create_application_version(account_key, application, label)
        self._client(account_key, "elasticbeanstalk").create_application_version(
            ApplicationName=application,
            VersionLabel=label,
            Description=description[:200],
            SourceBundle={"S3Bucket": bucket, "S3Key": key},
            Process=True,
            AutoCreateApplication=False,
        )

    @_wrap_errors
    def application_version_status(self, account_key: str, application: str, label: str) -> str:
        if self._mock:
            return self._mock.application_version_status(account_key, application, label)
        versions = self._client(account_key, "elasticbeanstalk").describe_application_versions(
            ApplicationName=application, VersionLabels=[label]
        ).get("ApplicationVersions", [])
        return versions[0]["Status"] if versions else "UNKNOWN"

    @_wrap_errors
    def update_environment(self, account_key: str, env_name: str, label: str) -> None:
        if self._mock:
            return self._mock.update_environment(account_key, env_name, label)
        self._client(account_key, "elasticbeanstalk").update_environment(EnvironmentName=env_name, VersionLabel=label)

    @_wrap_errors
    def describe_events(self, account_key: str, env_name: str, since: datetime) -> list[dict[str, str]]:
        if self._mock:
            return self._mock.describe_events(account_key, env_name, since)
        events = self._client(account_key, "elasticbeanstalk").describe_events(
            EnvironmentName=env_name, StartTime=since, MaxRecords=100
        ).get("Events", [])
        return [
            {"time": e["EventDate"].isoformat(), "severity": e.get("Severity", "INFO"), "message": e.get("Message", "")}
            for e in reversed(events)
        ]


class MockBeanstalk:
    """Thread-safe simulator mirroring the AwsService EB surface for dry-run mode."""

    VERSION_PROCESS_SECONDS = 2
    _TIMELINE = (
        (0, "INFO", "Environment update is starting."),
        (2, "INFO", "Deploying new version to instance(s)."),
        (5, "INFO", "Instance deployment used the commands in your 'Procfile' to initiate startup of your application."),
        (8, "INFO", "Instance deployment completed successfully."),
        (10, "INFO", "New application version was deployed to running EC2 instances."),
        (12, "INFO", "Environment update completed successfully."),
    )

    def __init__(self, accounts: dict[str, AwsAccount]) -> None:
        self._lock = threading.Lock()
        self._envs: dict[str, dict[str, dict[str, Any]]] = {}
        self._versions: dict[tuple[str, str, str], datetime] = {}
        self._events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        now = _utcnow()
        for key in accounts:
            self._envs[key] = {}
            for suffix, app, health in (("web", "cmt-web-portal", "Green"),
                                        ("api", "cmt-core-api", "Yellow"),
                                        ("worker", "cmt-batch-worker", "Green")):
                name = f"{key}-{suffix}"
                self._envs[key][name] = {
                    "name": name,
                    "application": app,
                    "status": "Ready",
                    "health": health,
                    "health_status": "Ok" if health == "Green" else "Warning",
                    "version_label": "v2026.10.01-a1b2c3",
                    "cname": f"{name}.us-east-1.elasticbeanstalk.com",
                    "platform": "64bit Amazon Linux 2023 v4.3.0 running Python 3.11",
                    "tier": "WebServer" if suffix != "worker" else "Worker",
                    "updated": (now - timedelta(days=3)).isoformat(),
                }

    def _env(self, account_key: str, env_name: str) -> dict[str, Any]:
        env = self._envs.get(account_key, {}).get(env_name)
        if env is None:
            raise AwsError(f"Environment '{env_name}' not found.", 404)
        deploy = env.get("_deploy")
        if deploy and _utcnow() >= deploy["finish_at"]:
            env.update(status="Ready", health="Green", health_status="Ok",
                       version_label=deploy["label"], updated=_utcnow().isoformat())
            env.pop("_deploy")
        return env

    @staticmethod
    def _public(env: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in env.items() if not k.startswith("_")}

    def list_environments(self, account_key: str) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(self._env(account_key, n)) for n in sorted(self._envs.get(account_key, {}))]

    def get_environment(self, account_key: str, env_name: str) -> dict[str, Any]:
        with self._lock:
            return self._public(self._env(account_key, env_name))

    def upload_bundle(self, path: Path, key: str) -> tuple[str, str]:
        if not path.is_file():
            raise AwsError("Bundle file is missing.", 500)
        return "elasticbeanstalk-dry-run-bucket", key

    def create_application_version(self, account_key: str, application: str, label: str) -> None:
        with self._lock:
            self._versions[(account_key, application, label)] = _utcnow()

    def application_version_status(self, account_key: str, application: str, label: str) -> str:
        with self._lock:
            created = self._versions.get((account_key, application, label))
        if created is None:
            return "UNKNOWN"
        return "PROCESSED" if (_utcnow() - created).total_seconds() >= self.VERSION_PROCESS_SECONDS else "PROCESSING"

    def update_environment(self, account_key: str, env_name: str, label: str) -> None:
        with self._lock:
            env = self._env(account_key, env_name)
            if env["status"] != "Ready":
                raise AwsError(f"Environment is {env['status']}; cannot update now.", 409)
            start = _utcnow()
            env.update(status="Updating", health="Grey", health_status="Info")
            env["_deploy"] = {"label": label, "finish_at": start + timedelta(seconds=self._TIMELINE[-1][0])}
            self._events.setdefault((account_key, env_name), []).extend(
                {"at": start + timedelta(seconds=offset), "severity": sev, "message": msg}
                for offset, sev, msg in self._TIMELINE
            )

    def describe_events(self, account_key: str, env_name: str, since: datetime) -> list[dict[str, str]]:
        now = _utcnow()
        with self._lock:
            events = self._events.get((account_key, env_name), [])
            return [
                {"time": e["at"].isoformat(), "severity": e["severity"], "message": e["message"]}
                for e in events if since <= e["at"] <= now
            ]

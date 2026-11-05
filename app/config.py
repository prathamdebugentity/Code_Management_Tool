from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AwsAccount:
    key: str
    name: str
    region: str
    role_arn: str | None = None
    external_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class Settings:
    app_username: str
    app_password: str
    dry_run_mode: str  # "auto" | "true" | "false"
    session_ttl_seconds: int
    cookie_secure: bool
    max_upload_mb: int
    max_uncompressed_mb: int
    deploy_timeout_seconds: int
    poll_interval_seconds: float
    github_api_url: str
    accounts: dict[str, AwsAccount]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_uncompressed_bytes(self) -> int:
        return self.max_uncompressed_mb * 1024 * 1024

    @property
    def using_default_credentials(self) -> bool:
        return self.app_username == "admin" and self.app_password == "admin"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_accounts(default_region: str) -> dict[str, AwsAccount]:
    raw = os.getenv("AWS_ACCOUNTS_JSON")
    if raw:
        accounts: dict[str, AwsAccount] = {}
        for item in json.loads(raw):
            accounts[item["key"]] = AwsAccount(
                key=item["key"],
                name=item.get("name", item["key"]),
                region=item.get("region", default_region),
                role_arn=item.get("role_arn") or None,
                external_id=item.get("external_id") or None,
                account_id=item.get("account_id") or None,
            )
        return accounts

    accounts = {}
    for key, name in (("dev", "Development"), ("staging", "Staging"), ("prod", "Production")):
        prefix = f"AWS_{key.upper()}_"
        accounts[key] = AwsAccount(
            key=key,
            name=name,
            region=os.getenv(prefix + "REGION", default_region),
            role_arn=os.getenv(prefix + "ROLE_ARN") or None,
            external_id=os.getenv(prefix + "EXTERNAL_ID") or None,
            account_id=os.getenv(prefix + "ACCOUNT_ID") or None,
        )
    return accounts


@lru_cache
def get_settings() -> Settings:
    mode = os.getenv("DRY_RUN", "auto").strip().lower()
    if mode not in {"auto", "true", "false"}:
        raise ValueError("DRY_RUN must be one of: auto, true, false")
    default_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    return Settings(
        app_username=os.getenv("APP_USERNAME", "admin"),
        app_password=os.getenv("APP_PASSWORD", "admin"),
        dry_run_mode=mode,
        session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "28800")),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "200")),
        max_uncompressed_mb=int(os.getenv("MAX_UNCOMPRESSED_MB", "1024")),
        deploy_timeout_seconds=int(os.getenv("DEPLOY_TIMEOUT_SECONDS", "1800")),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "5")),
        github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        accounts=_load_accounts(default_region),
    )

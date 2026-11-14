"""Settings, read from environment variables or .env (12-factor). See README, "Configuration"."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Role = Literal["approver", "viewer"]
MIN_SECRET_LENGTH = 32
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


class Approver(BaseModel):
    """One person who can sign in. `hash` is an argon2id hash (python -m approval hash-password)."""

    name: str = Field(min_length=1, max_length=80)
    hash: str = Field(min_length=20)
    role: Role = "approver"
    limit: Decimal | None = Field(default=None, gt=0, description="Largest total this person may approve")

    @field_validator("hash")
    @classmethod
    def _argon2(cls, value: str) -> str:
        if not value.startswith("$argon2"):
            raise ValueError("password hashes must be argon2 (run: python -m approval hash-password)")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    database_path: str = "data/approval.db"
    migrate_on_start: bool = True

    # People
    approvers: dict[str, Approver] = Field(default_factory=dict)
    session_ttl_minutes: int = Field(default=480, ge=5, le=7 * 24 * 60)
    cookie_secure: bool = True
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_window_minutes: int = Field(default=15, ge=1, le=24 * 60)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    trust_proxy_headers: bool = False
    max_body_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)

    # Drafts
    draft_ttl_hours: int = Field(default=48, ge=1, le=24 * 30)
    max_draft_total: Decimal = Field(default=Decimal("50000.00"), gt=0)
    max_lines: int = Field(default=50, ge=1, le=500)
    max_quantity: int = Field(default=10_000, ge=1, le=1_000_000)
    page_size: int = Field(default=25, ge=5, le=200)

    # Extraction (Claude)
    extractor: Literal["claude", "demo"] = "claude"
    extract_model: str = "claude-haiku-5-5"
    extract_effort: Effort = "low"
    extract_max_tokens: int = Field(default=4_000, ge=512, le=32_000)
    extract_day_cap_usd: Decimal = Field(default=Decimal("1.00"), gt=0, le=1_000)
    max_email_chars: int = Field(default=20_000, ge=500, le=200_000)
    request_timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)

    # Posting
    post_target: Literal["local", "governed-write-api"] = "local"
    writeapi_url: str = "http://127.0.0.1:8001"
    writeapi_key_id: str = ""
    writeapi_secret: str = ""
    writeapi_due_in_days: int = Field(default=30, ge=0, le=180)
    writeapi_timeout_seconds: float = Field(default=10.0, ge=1, le=120)

    log_level: str = "INFO"

    @field_validator("approvers")
    @classmethod
    def _usernames(cls, approvers: dict[str, Approver]) -> dict[str, Approver]:
        for username in approvers:
            if not _USERNAME.match(username):
                raise ValueError(f"username {username!r}: 2-32 of a-z 0-9 . _ - (lower case)")
        return approvers

    @model_validator(mode="after")
    def _writeapi_credentials(self) -> Settings:
        if self.post_target == "governed-write-api":
            if not self.writeapi_key_id:
                raise ValueError("POST_TARGET=governed-write-api needs WRITEAPI_KEY_ID")
            if len(self.writeapi_secret) < MIN_SECRET_LENGTH or "replace-with" in self.writeapi_secret:
                raise ValueError(f"WRITEAPI_SECRET must be a real secret of {MIN_SECRET_LENGTH}+ characters")
        return self

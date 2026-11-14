from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from approval import catalogue as cat
from approval.auth import hash_password
from approval.catalogue import Catalogue
from approval.config import Approver, Settings
from approval.db import Database
from approval.drafts import DraftService, User
from approval.posting import LocalPoster
from tests.fakes import Clock

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_orders"
PASSWORD = "correct horse battery staple"

# Never let a test reach the real API, whatever the developer's shell has set.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(PASSWORD)  # argon2 is slow on purpose: hash once per test session


@pytest.fixture
def approvers(password_hash) -> dict[str, Approver]:
    return {
        "dana": Approver(name="Dana Whitfield", hash=password_hash, role="approver", limit=Decimal("20000.00")),
        "lee": Approver(name="Lee Okafor", hash=password_hash, role="approver"),
        "sam": Approver(name="Sam Ortega", hash=password_hash, role="viewer"),
    }


@pytest.fixture
def settings(tmp_path, approvers) -> Settings:
    return Settings(
        _env_file=None, database_path=str(tmp_path / "approval.db"), approvers=approvers, cookie_secure=False
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(settings):
    d = Database(settings.database_path)
    d.migrate()
    cat.seed(d)
    yield d
    d.close()


@pytest.fixture
def catalogue(db) -> Catalogue:
    return Catalogue(db)


@pytest.fixture
def service(settings, db, catalogue, clock) -> DraftService:
    return DraftService(settings, db, catalogue, LocalPoster(db, clock), clock)


@pytest.fixture
def dana(settings) -> User:
    a = settings.approvers["dana"]
    return User("dana", a.name, a.role, a.limit)


@pytest.fixture
def lee(settings) -> User:
    a = settings.approvers["lee"]
    return User("lee", a.name, a.role, a.limit)


@pytest.fixture
def sam(settings) -> User:
    return User("sam", "Sam Ortega", "viewer")

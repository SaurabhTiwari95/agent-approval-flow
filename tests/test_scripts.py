import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from approval.posting import LocalPoster
from approval.web import create_app
from tests.conftest import PASSWORD
from tests.test_drafts import kestrel, new_draft

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import smoke  # noqa: E402


def test_smoke_approves_the_first_approvable_draft(settings, db, clock):
    app = create_app(settings, db, LocalPoster(db, clock), clock)
    service = app.state.approval.drafts
    blocked = new_draft(service, kestrel(customer_email=None), text="no customer")
    ok = new_draft(service, text="fine")
    client = TestClient(app, follow_redirects=False)
    assert smoke.run("", "dana", PASSWORD, client) == f"draft {ok} approved and posted as INV-000001"
    assert service.get(blocked)["status"] == "pending"


def test_smoke_reports_failures(settings, db, clock, monkeypatch, capsys):
    app = create_app(settings, db, LocalPoster(db, clock), clock)
    client = TestClient(app, follow_redirects=False)
    with pytest.raises(RuntimeError, match="sign-in failed"):
        smoke.run("", "dana", "wrong password", client)
    with pytest.raises(RuntimeError, match="no pending draft"):
        smoke.run("", "dana", PASSWORD, TestClient(app, follow_redirects=False))
    monkeypatch.delenv("SMOKE_PASSWORD", raising=False)
    assert smoke.main([]) == 2
    monkeypatch.setenv("SMOKE_PASSWORD", "x")
    assert smoke.main(["--url", "http://127.0.0.1:9"]) == 1  # nothing listens there

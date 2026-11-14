from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from approval.extractor import DemoExtractor
from approval.posting import LocalPoster
from approval.web import RateLimiter, create_app
from tests.conftest import PASSWORD
from tests.test_drafts import ExtractedLine, kestrel, new_draft

CSRF = re.compile(r'name="csrf" value="([^"]+)"')
VERSION = re.compile(r'name="version" value="(\d+)"')


@pytest.fixture
def app(settings, db, clock):
    return create_app(settings, db, LocalPoster(db, clock), clock)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def service(app):
    return app.state.approval.drafts


def csrf_of(html: str) -> str:
    return CSRF.search(html).group(1)


def login(client, username="dana", password=PASSWORD):
    page = client.get("/login")
    return client.post("/login", data={"username": username, "password": password, "csrf": csrf_of(page.text)})


def signed_in(client, username="dana") -> str:
    r = login(client, username)
    assert r.status_code == 303, r.text
    return csrf_of(client.get("/drafts").text)


# --- sign-in -------------------------------------------------------------------------------------


def test_pages_need_sign_in(client):
    for path in ("/drafts", "/drafts/1", "/"):
        r = client.get(path)
        assert r.status_code == 303
    assert client.get("/drafts").headers["location"] == "/login"


def test_login_sets_a_safe_session_cookie(client):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/drafts"
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("aaf_session=")][0].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    page = client.get("/drafts")
    assert page.status_code == 200 and "Dana Whitfield" in page.text
    assert client.get("/login").status_code == 303  # already signed in


def test_secure_cookies_use_host_prefix(settings, db, clock):
    app = create_app(settings.model_copy(update={"cookie_secure": True}), db, LocalPoster(db, clock), clock)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    r = login(c)
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("__Host-aaf_session=")][0].lower()
    assert "secure" in cookie and "httponly" in cookie
    assert "max-age=31536000" in c.get("/drafts").headers["strict-transport-security"]


def test_wrong_password_and_unknown_user_look_the_same(client):
    a = login(client, "dana", "wrong password!")
    b = login(client, "nobody", "wrong password!")
    assert a.status_code == b.status_code == 401
    assert "Wrong username or password." in a.text and "Wrong username or password." in b.text
    assert "set-cookie" not in a.headers or "aaf_session" not in a.headers["set-cookie"]


def test_login_needs_the_form_csrf_token(client):
    client.get("/login")
    r = client.post("/login", data={"username": "dana", "password": PASSWORD, "csrf": "forged"})
    assert r.status_code == 403 and "expired" in r.text


def test_login_is_throttled_after_repeated_failures(client, settings):
    for _ in range(settings.login_max_failures):
        assert login(client, "dana", "bad guess 123").status_code == 401
    r = login(client, "dana", PASSWORD)  # even the right password is refused while throttled
    assert r.status_code == 429


def test_throttle_window_passes(client, settings, clock):
    for _ in range(settings.login_max_failures):
        login(client, "lee", "bad guess 123")
    assert login(client, "lee").status_code == 429
    clock.advance(minutes=settings.login_window_minutes + 1)
    assert login(client, "lee").status_code == 303


def test_passwords_never_logged(client, caplog):
    caplog.set_level("DEBUG")
    login(client, "dana", "s3cret-typo-password")
    login(client)
    assert "s3cret-typo-password" not in caplog.text and PASSWORD not in caplog.text


def test_logout(client):
    csrf = signed_in(client)
    r = client.post("/logout", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/login?m=signed_out"
    assert "signed out" in client.get("/login?m=signed_out").text
    assert client.get("/drafts").status_code == 303


def test_session_expires(client, clock, settings):
    signed_in(client)
    clock.advance(minutes=settings.session_ttl_minutes + 1)
    assert client.get("/drafts").status_code == 303


def test_removed_approver_loses_access(client, app):
    signed_in(client, "lee")
    del app.state.approval.settings.approvers["lee"]
    assert client.get("/drafts").status_code == 303


def test_session_cleanup(client, app, clock, settings):
    login(client, "dana", "bad guess 123")
    signed_in(client)
    clock.advance(minutes=settings.session_ttl_minutes + 1)
    assert app.state.approval.auth.cleanup() == 2


# --- pages ---------------------------------------------------------------------------------------


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "script-src" not in r.headers["content-security-policy"]  # no scripts at all
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "<script" not in r.text


def test_list_and_detail(client, service):
    draft_id = new_draft(service)
    signed_in(client)
    listing = client.get("/drafts")
    assert "Kestrel Kitchenware" in listing.text and "649.00" in listing.text and "48h 00m" in listing.text
    page = client.get(f"/drafts/{draft_id}")
    assert page.status_code == 200
    assert "Original email" in page.text and "Proposed invoice" in page.text
    assert "Approve and post as Dana Whitfield" in page.text
    assert "Audit trail" in page.text and "extracted" in page.text
    assert client.get("/drafts?status=bogus").status_code == 200
    assert client.get("/drafts?status=all&page_no=2").status_code == 200


def test_email_content_is_escaped(client, service):
    draft_id = service.ingest("x.txt", "<script>alert(1)</script> SKU-00001 x 2", DemoExtractor()).draft_id
    signed_in(client)
    page = client.get(f"/drafts/{draft_id}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_unknown_draft_404(client):
    signed_in(client)
    r = client.get("/drafts/999")
    assert r.status_code == 404 and "No such draft" in r.text
    assert client.get("/nope").status_code == 404


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready", "database": True}


def test_not_ready_without_schema(settings, tmp_path, clock):
    from approval.db import Database

    s = settings.model_copy(update={"migrate_on_start": False, "database_path": str(tmp_path / "empty.db")})
    app = create_app(s, Database(s.database_path), None, clock)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503


# --- actions through the forms -------------------------------------------------------------------


def test_approve_through_the_page(client, service, db):
    draft_id = new_draft(service)
    signed_in(client)
    page = client.get(f"/drafts/{draft_id}").text
    r = client.post(
        f"/drafts/{draft_id}/approve", data={"csrf": csrf_of(page), "version": VERSION.search(page).group(1)}
    )
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{draft_id}?m=posted"
    done = client.get(r.headers["location"]).text
    assert "Approved and posted under your name." in done and "INV-000001" in done
    # submitting the same form again (double click, back button) posts nothing more
    again = client.post(
        f"/drafts/{draft_id}/approve", data={"csrf": csrf_of(page), "version": VERSION.search(page).group(1)}
    )
    assert again.status_code == 409 and "This draft is posted." in again.text
    assert db.one("SELECT count(*) AS n FROM invoices")["n"] == 1


def test_post_without_csrf_is_refused(client, service):
    draft_id = new_draft(service)
    signed_in(client)
    r = client.post(f"/drafts/{draft_id}/approve", data={"csrf": "forged", "version": "1"})
    assert r.status_code == 403
    assert client.post(f"/drafts/{draft_id}/approve", data={"version": "1"}).status_code == 422
    assert service.get(draft_id)["status"] == "pending"


def test_post_without_session_redirects(client, service):
    draft_id = new_draft(service)
    r = client.post(f"/drafts/{draft_id}/approve", data={"csrf": "x", "version": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_viewer_sees_but_cannot_act(client, service):
    draft_id = new_draft(service)
    csrf = signed_in(client, "sam")
    page = client.get(f"/drafts/{draft_id}").text
    assert "Only approvers can" in page and "/approve" not in page
    r = client.post(f"/drafts/{draft_id}/approve", data={"csrf": csrf, "version": "1"})
    assert r.status_code == 403
    assert service.get(draft_id)["status"] == "pending"


def test_edit_through_the_page(client, service):
    draft_id = new_draft(service, kestrel(customer_email=None))
    csrf = signed_in(client, "lee")
    data = {
        "csrf": csrf,
        "version": "1",
        "customer_id": "3",
        "sku": ["SKU-00001", "SKU-00003", "SKU-00011", ""],
        "qty": ["24", "0", "2", ""],
    }
    r = client.post(f"/drafts/{draft_id}/edit", data=data)
    assert r.status_code == 303 and r.headers["location"].endswith("?m=edited")
    d = service.get(draft_id)
    assert d["customer_id"] == 3 and [(ln["sku"], ln["quantity"]) for ln in d["lines"]] == [
        ("SKU-00001", 24),
        ("SKU-00011", 2),
    ]


def test_edit_errors_are_shown(client, service):
    draft_id = new_draft(service)
    csrf = signed_in(client, "lee")
    r = client.post(
        f"/drafts/{draft_id}/edit", data={"csrf": csrf, "version": "1", "sku": ["SKU-00001"], "qty": ["two"]}
    )
    assert r.status_code == 422 and "must be a whole number" in r.text
    r = client.post(f"/drafts/{draft_id}/edit", data={"csrf": csrf, "version": "9", "sku": ["SKU-00001"], "qty": ["2"]})
    assert r.status_code == 409 and "changed since you opened it" in r.text


def test_reject_through_the_page(client, service):
    draft_id = new_draft(service)
    csrf = signed_in(client)
    r = client.post(f"/drafts/{draft_id}/reject", data={"csrf": csrf, "version": "1", "reason": ""})
    assert r.status_code == 422 and "Give a reason" in r.text
    r = client.post(f"/drafts/{draft_id}/reject", data={"csrf": csrf, "version": "1", "reason": "Duplicate of PO 4471"})
    assert r.status_code == 303
    assert "Duplicate of PO 4471" in client.get(f"/drafts/{draft_id}").text


def test_limit_blocks_the_button_and_the_post(client, service):
    big = kestrel(lines=[ExtractedLine(sku="SKU-00012", sku_source="email", product_name="k", quantity=400)])
    draft_id = new_draft(service, big)
    csrf = signed_in(client)  # dana: limit 20,000
    page = client.get(f"/drafts/{draft_id}").text
    assert "over your approval limit" in page and "disabled" in page
    r = client.post(f"/drafts/{draft_id}/approve", data={"csrf": csrf, "version": "1"})
    assert r.status_code == 422


def test_expired_draft_page(client, service, clock):
    draft_id = new_draft(service)
    clock.advance(hours=49)
    csrf = signed_in(client)
    assert "expired" in client.get(f"/drafts/{draft_id}").text
    r = client.post(f"/drafts/{draft_id}/approve", data={"csrf": csrf, "version": "1"})
    assert r.status_code == 410 and "has expired" in r.text


def test_post_failed_message(settings, db, clock, dana):
    from approval.posting import PostResult

    class Down:
        target = "down"

        def post(self, req):
            return PostResult(False, error="unreachable")

    app = create_app(settings, db, Down(), clock)
    c = TestClient(app, follow_redirects=False)
    draft_id = new_draft(app.state.approval.drafts)
    csrf = signed_in(c)
    r = c.post(f"/drafts/{draft_id}/approve", data={"csrf": csrf, "version": "1"})
    assert r.headers["location"].endswith("?m=post_failed")
    page = c.get(r.headers["location"]).text
    assert "posting failed" in page and "Retry posting" in page


def test_unknown_draft_action_404(client):
    csrf = signed_in(client)
    assert client.post("/drafts/999/approve", data={"csrf": csrf, "version": "1"}).status_code == 404


# --- limits --------------------------------------------------------------------------------------


def test_body_size_limit(client, settings):
    csrf = signed_in(client)
    r = client.post(
        "/drafts/1/reject", data={"csrf": csrf, "version": "1", "reason": "x" * (settings.max_body_bytes + 10)}
    )
    assert r.status_code == 413


def test_rate_limit(client, app):
    app.state.approval.limiter.per_minute = 3
    codes = [client.get("/login").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert client.get("/healthz").status_code == 200  # health checks are never limited


def test_rate_limiter_window_resets():
    now = [0.0]
    limiter = RateLimiter(1, clock=lambda: now[0])
    assert limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")
    now[0] = 61
    assert limiter.allow("a")


def test_proxy_header_only_when_trusted(settings, db, clock):
    s = settings.model_copy(update={"trust_proxy_headers": True})
    app = create_app(s, db, LocalPoster(db, clock), clock)
    app.state.approval.limiter.per_minute = 1
    c = TestClient(app)
    assert c.get("/login", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    assert c.get("/login", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 200
    assert c.get("/login", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 429

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest

from approval import signing
from approval.drafts import DraftService
from approval.posting import GovernedWriteApiPoster, LocalPoster, PostRequest, WriteApiClient, WriteApiError
from approval.web import build_poster
from tests.fakes import FakeWriteApi
from tests.test_drafts import kestrel, new_draft

KEY_ID, SECRET = "approval-flow", "s" * 40
# Same prices as this app's sample catalogue for SKU-00001 and SKU-00003
REMOTE_PRICES = {1: "12.50", 3: "34.90"}


def governed(api: FakeWriteApi) -> GovernedWriteApiPoster:
    client = WriteApiClient("http://writeapi.test", KEY_ID, SECRET, transport=httpx.MockTransport(api))
    return GovernedWriteApiPoster(client, due_in_days=30)


def request(state=None, total="649.00") -> PostRequest:
    return PostRequest(
        draft_id=7,
        idempotency_key="k" * 32,
        customer_id=1,
        lines=[{"product_id": 1, "quantity": 24}, {"product_id": 3, "quantity": 10}],
        total=Decimal(total),
        issued_on=dt.date(2026, 11, 9),
        approver="dana",
        approver_name="Dana Whitfield",
        drafted_by="claude-opus-5-5",
        state=state or {},
    )


def test_signed_validate_then_commit_with_idempotency_key():
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    result = governed(api).post(request())
    assert result.ok and result.reference == "writeapi-invoice-6001"
    validate, commit = api.requests
    assert validate.url.path == "/v1/validate/create_invoice"
    assert json.loads(validate.content)["params"] == {
        "customer_id": 1,
        "issued_on": "2026-11-09",
        "due_in_days": 30,
        "lines": [{"product_id": 1, "quantity": 24}, {"product_id": 3, "quantity": 10}],
    }
    assert commit.headers["Idempotency-Key"] == "k" * 32
    for r in api.requests:  # every request is signed with the shared scheme
        h = r.headers
        assert signing.verify(SECRET, h["X-Signature"], "POST", r.url.path, h["X-Timestamp"], h["X-Nonce"], r.content)


def test_price_mismatch_never_commits():
    api = FakeWriteApi(KEY_ID, SECRET, {1: "13.00", 3: "34.90"})
    result = governed(api).post(request())
    assert not result.ok and result.error == "price_mismatch"
    assert result.detail == {"approved": "649.00", "remote": "661.00"}
    assert api.invoices == [] and len(api.requests) == 1


def test_lost_commit_response_retry_replays_not_duplicates():
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    poster = governed(api)
    real = poster.client.commit

    def commit_then_drop(token, key):
        real(token, key)  # the write happens...
        raise httpx.ReadTimeout("response lost")  # ...but the answer never arrives

    poster.client.commit = commit_then_drop
    first = poster.post(request())
    assert not first.ok and first.error == "unreachable" and first.state["token"]
    poster.client.commit = real
    second = poster.post(request(state=first.state))
    assert second.ok and second.detail["replayed"] is True
    assert len(api.invoices) == 1


def test_temporary_error_keeps_token_and_key():
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    poster = governed(api)
    real = poster.client.commit

    def breaker_open(token, key):
        raise WriteApiError(503, {"error": "circuit_open"})

    poster.client.commit = breaker_open
    first = poster.post(request())
    assert first.error == "circuit_open" and "token" in first.state
    poster.client.commit = real
    assert poster.post(request(state=first.state)).ok
    assert len(api.invoices) == 1


def test_dead_token_starts_over_with_a_new_key():
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    poster = governed(api)
    first = poster.post(request(state={"attempt": 1, "token": "tok-gone", "key": "k" * 32}))
    assert not first.ok and first.error == "unknown_token" and first.state == {"attempt": 1}
    second = poster.post(request(state=first.state))
    assert second.ok
    assert api.requests[-1].headers["Idempotency-Key"] == "k" * 32 + "-r2"


def test_validation_refused_and_unreachable():
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    api.fail_next = [(422, "rule_violation")]
    result = governed(api).post(request())
    assert (result.ok, result.error, result.state) == (False, "rule_violation", {"attempt": 1})

    api.fail_next = [httpx.ConnectError("down")]
    result = governed(api).post(request())
    assert result.error == "unreachable"


def test_commit_transport_error_keeps_state():
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    poster = governed(api)
    api_calls = []

    def handler(req):
        api_calls.append(req)
        if req.url.path == "/v1/commit":
            raise httpx.ConnectError("down")
        return api(req)

    poster.client = WriteApiClient("http://writeapi.test", KEY_ID, SECRET, transport=httpx.MockTransport(handler))
    result = poster.post(request())
    assert result.error == "unreachable" and result.state["key"] == "k" * 32


def test_wrong_secret_is_unauthorized():
    api = FakeWriteApi(KEY_ID, "x" * 40, REMOTE_PRICES)
    result = governed(api).post(request())
    assert result.error == "unauthorized"


def test_non_json_error_body():
    client = WriteApiClient(
        "http://writeapi.test",
        KEY_ID,
        SECRET,
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")),
    )
    result = GovernedWriteApiPoster(client).post(request())
    assert result.error == "non_json_response"
    client.close()


def test_end_to_end_approval_through_governed_api(settings, db, catalogue, clock, dana):
    api = FakeWriteApi(KEY_ID, SECRET, REMOTE_PRICES)
    service = DraftService(settings, db, catalogue, governed(api), clock)
    draft_id = new_draft(service, kestrel())
    d = service.approve(draft_id, 1, dana)
    assert d["status"] == "posted" and d["posted_ref"] == "writeapi-invoice-6001"
    trail = service.audit_trail(draft_id)
    assert trail[-1]["event"] == "posted" and trail[-1]["detail"]["target"] == "governed-write-api"
    assert db.one("SELECT count(*) AS n FROM invoices")["n"] == 0  # posted remotely, not locally


def test_local_poster_is_idempotent(db, service):
    draft_id = new_draft(service)
    req = request()
    req = PostRequest(**{**req.__dict__, "draft_id": draft_id})
    poster = LocalPoster(db)
    assert poster.post(req).reference == poster.post(req).reference
    assert db.one("SELECT count(*) AS n FROM invoices")["n"] == 1


def test_build_poster_from_settings(settings, db):
    assert isinstance(build_poster(settings, db), LocalPoster)
    s = settings.model_copy(
        update={"post_target": "governed-write-api", "writeapi_key_id": KEY_ID, "writeapi_secret": SECRET}
    )
    assert isinstance(build_poster(s, db), GovernedWriteApiPoster)


@pytest.mark.parametrize("secret", ["short", "replace-with-a-long-random-secret-at-least-32-characters"])
def test_governed_target_needs_a_real_secret(secret):
    from approval.config import Settings

    with pytest.raises(ValueError, match="WRITEAPI_SECRET"):
        Settings(_env_file=None, post_target="governed-write-api", writeapi_key_id="a", writeapi_secret=secret)
    with pytest.raises(ValueError, match="WRITEAPI_KEY_ID"):
        Settings(_env_file=None, post_target="governed-write-api", writeapi_secret=SECRET)

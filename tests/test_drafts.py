from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal

import pytest

from approval.drafts import DraftError, DraftService
from approval.extractor import DemoExtractor, ExtractedLine, Extraction, ExtractResult
from approval.posting import LocalPoster, PostResult
from tests.conftest import SAMPLES


class Scripted:
    """An extractor that returns a fixed Extraction (or a failure status)."""

    name = "claude-opus-5-5"

    def __init__(self, extraction: Extraction | None = None, status: str = "ok", detail: str = ""):
        self.extraction, self.status, self.detail = extraction, status, detail
        self.calls = 0

    def extract(self, source_name, email_text):
        self.calls += 1
        return ExtractResult(self.status, self.extraction, self.name, Decimal("0.0123"), detail=self.detail)


def kestrel(**kw) -> Extraction:
    data = {
        "customer_email": "purchasing@kestrel.example.com",
        "lines": [
            ExtractedLine(sku="SKU-00001", sku_source="email", product_name="Steel Mixing Bowl 3L", quantity=24),
            ExtractedLine(sku="SKU-00003", sku_source="email", product_name="Cast Iron Skillet 26cm", quantity=10),
        ],
        "requested_date": "2026-11-16",
        "confidence": "high",
    }
    data.update(kw)
    return Extraction(**data)


def new_draft(service, extraction=None, text="order email", name="a.txt") -> int:
    out = service.ingest(name, text, Scripted(extraction or kestrel()))
    assert out.status == "created"
    return out.draft_id


def events(db, draft_id):
    return [r["event"] for r in db.all("SELECT event FROM audit_log WHERE draft_id = ? ORDER BY id", (draft_id,))]


# --- ingestion -----------------------------------------------------------------------------------


def test_ingest_prices_lines_on_the_server(service, db):
    draft_id = new_draft(service)
    d = service.get(draft_id)
    assert d["status"] == "pending" and d["customer"].name == "Kestrel Kitchenware"
    assert [(ln["sku"], ln["quantity"], ln["unit_price"], ln["line_total"]) for ln in d["lines"]] == [
        ("SKU-00001", 24, "12.50", "300.00"),
        ("SKU-00003", 10, "34.90", "349.00"),
    ]
    assert d["total"] == "649.00" and d["requested_date"] == "2026-11-16"
    assert d["expires_at"] == "2026-11-11T09:00:00Z"  # 48 hours
    assert d["extraction_usd"] == "0.0123" and d["extracted_by"] == "claude-opus-5-5"
    assert events(db, draft_id) == ["extracted"]


def test_ingest_same_email_twice_calls_the_agent_once(service):
    agent = Scripted(kestrel())
    first = service.ingest("a.txt", "same text", agent)
    second = service.ingest("b.txt", "same text", agent)
    assert (first.status, second.status, second.draft_id) == ("created", "duplicate", first.draft_id)
    assert agent.calls == 1


def test_ingest_flags_everything_it_cannot_match(service):
    ex = kestrel(
        customer_email="someone@unknown.example.com",
        customer_name="Unknown Ltd",
        lines=[
            ExtractedLine(sku=None, sku_source="none", product_name="garden hose", quantity=5),
            ExtractedLine(sku="SKU-00002", sku_source="email", product_name="board", quantity=0),
            ExtractedLine(sku="SKU-00009", sku_source="catalogue_match", product_name="blue mugs", quantity=12),
            ExtractedLine(sku=None, sku_source="none", product_name="enamel mug", quantity=3),
        ],
        requested_date="next friday",
    )
    d = service.get(new_draft(service, ex))
    codes = [p["code"] for p in d["problems"]]
    assert codes == ["unknown_customer", "unknown_product", "bad_quantity", "inferred_sku", "bad_date"]
    # the same product twice (by SKU and by exact name) becomes one line
    assert [(ln["sku"], ln["quantity"]) for ln in d["lines"]] == [("SKU-00009", 15)]
    assert d["customer_id"] is None and d["requested_date"] is None


def test_customer_matched_by_name_when_no_email(service):
    d = service.get(new_draft(service, kestrel(customer_email=None, customer_name="  northgate   hardware co. ")))
    assert d["customer"].name == "Northgate Hardware Co."


def test_too_many_lines_are_cut_and_flagged(service, settings):
    service.s = settings.model_copy(update={"max_lines": 1})
    d = service.get(new_draft(service))
    assert len(d["lines"]) == 1 and "too_many_lines" in [p["code"] for p in d["problems"]]


def test_failed_extraction_still_reaches_a_person(service, db):
    out = service.ingest("a.txt", "hello", Scripted(None, status="refused", detail="declined"))
    d = service.get(out.draft_id)
    assert d["lines"] == [] and d["problems"][0]["code"] == "extraction_refused"
    assert events(db, out.draft_id) == ["extraction_failed"]


def test_day_cap_skips_without_a_draft(service, db):
    out = service.ingest("a.txt", "hello", Scripted(None, status="day_cap", detail="cap reached"))
    assert out.status == "skipped" and out.draft_id is None
    assert db.one("SELECT count(*) AS n FROM drafts")["n"] == 0


def test_too_large_email_is_refused(service, settings):
    out = service.ingest("a.txt", "x" * (settings.max_email_chars + 1), Scripted(kestrel()))
    assert out.status == "too_large"


def test_all_samples_with_demo_extractor(service):
    outcomes = [
        service.ingest(f.name, f.read_text(encoding="utf-8"), DemoExtractor()) for f in sorted(SAMPLES.iterdir())
    ]
    assert [o.status for o in outcomes] == ["created"] * 5
    assert [o.total for o in outcomes] == ["886.00", "0.00", "542.00", "326.70", "78000.00"]
    assert outcomes[1].problems == ["no_lines"] and outcomes[3].problems == ["unknown_customer"]


# --- approve -------------------------------------------------------------------------------------


def test_approve_posts_under_the_approvers_name(service, db, dana):
    draft_id = new_draft(service)
    d = service.approve(draft_id, 1, dana)
    assert d["status"] == "posted" and d["decided_by"] == "dana" and d["posted_ref"] == "INV-000001"
    inv = db.one("SELECT * FROM invoices WHERE draft_id = ?", (draft_id,))
    assert (inv["posted_by"], inv["posted_by_name"], inv["drafted_by"], inv["total"]) == (
        "dana",
        "Dana Whitfield",
        "claude-opus-5-5",
        "649.00",
    )
    assert inv["idempotency_key"] == d["post_key"]
    assert events(db, draft_id) == ["extracted", "approved", "posted"]


def test_double_approve_posts_once(service, db, dana, lee):
    draft_id = new_draft(service)
    service.approve(draft_id, 1, dana)
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 1, lee)
    assert e.value.code == "status_posted"
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, service.get(draft_id)["version"], lee)
    assert e.value.code == "status_posted"
    assert db.one("SELECT count(*) AS n FROM invoices")["n"] == 1


def test_concurrent_approvals_post_once(settings, db, catalogue, clock, dana, lee):
    gate = threading.Barrier(8)
    posted = []

    class SlowPoster(LocalPoster):
        def post(self, req):
            posted.append(req.approver)
            return super().post(req)

    service = DraftService(settings, db, catalogue, SlowPoster(db, clock), clock)
    draft_id = new_draft(service)
    results = []

    def go(user):
        gate.wait()
        try:
            results.append(service.approve(draft_id, 1, user)["status"])
        except DraftError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go, args=(u,)) for u in [dana, lee] * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(posted) == 1
    assert sorted(results).count("posted") == 1
    assert all(r in ("posted", "status_posting", "status_posted", "changed") for r in results)
    assert db.one("SELECT count(*) AS n FROM invoices")["n"] == 1


def test_approve_refuses_a_stale_version(service, dana, lee):
    draft_id = new_draft(service)
    service.edit(draft_id, 1, lee, 1, [("SKU-00001", 5)])
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 1, dana)
    assert e.value.code == "changed"


def test_expired_draft_cannot_be_approved(service, db, clock, dana):
    draft_id = new_draft(service)
    clock.advance(hours=48)
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 1, dana)
    assert (e.value.code, e.value.status) == ("expired", 410)
    assert service.get(draft_id)["status"] == "expired"
    assert events(db, draft_id) == ["extracted", "expired"]


def test_expiry_boundary_inside_the_transaction(service, clock, dana, monkeypatch):
    draft_id = new_draft(service)
    clock.advance(hours=48)
    monkeypatch.setattr(service, "sweep_expired", lambda draft_id=None: 0)  # sweep missed it
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 1, dana)
    assert e.value.code == "expired"


def test_sweep_marks_all_open_drafts(service, clock):
    a = new_draft(service, text="one")
    b = new_draft(service, text="two")
    clock.advance(hours=47)
    assert service.sweep_expired() == 0
    clock.advance(hours=1)
    assert service.sweep_expired() == 2
    assert {service.get(a)["status"], service.get(b)["status"]} == {"expired"}


def test_ceiling_and_personal_limit(service, settings, db, dana, lee):
    big = kestrel(lines=[ExtractedLine(sku="SKU-00012", sku_source="email", product_name="kettle", quantity=400)])
    draft_id = new_draft(service, big)  # 400 x 58.00 = 23,200: over Dana's 20,000 limit, under the ceiling
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 1, dana)
    assert e.value.code == "not_approvable" and "your approval limit" in e.value.message
    assert events(db, draft_id)[-1] == "approve_refused"  # recorded even though refused
    assert service.approve(draft_id, 1, lee)["status"] == "posted"

    huge = kestrel(lines=[ExtractedLine(sku="SKU-00012", sku_source="email", product_name="k", quantity=1000)])
    huge_id = new_draft(service, huge, text="huge")
    with pytest.raises(DraftError) as e:
        service.approve(huge_id, 1, lee)
    assert "per-draft ceiling of 50000.00" in e.value.message


def test_blockers_need_customer_and_lines(service, lee):
    draft_id = new_draft(service, kestrel(customer_email="x@nowhere.example.com", lines=[]))
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 1, lee)
    assert "Choose the customer." in e.value.message and "Add at least one line." in e.value.message


def test_viewer_cannot_act(service, sam):
    draft_id = new_draft(service)
    for action in (
        lambda: service.approve(draft_id, 1, sam),
        lambda: service.reject(draft_id, 1, sam, "nope nope"),
        lambda: service.edit(draft_id, 1, sam, 1, [("SKU-00001", 1)]),
    ):
        with pytest.raises(DraftError) as e:
            action()
        assert e.value.status == 403


def test_unknown_draft(service, dana):
    with pytest.raises(DraftError) as e:
        service.get(999)
    assert e.value.status == 404
    with pytest.raises(DraftError) as e:
        service.approve(999, 1, dana)
    assert e.value.status == 404


# --- posting failures and retries ----------------------------------------------------------------


class FlakyPoster:
    target = "flaky"

    def __init__(self, outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def post(self, req):
        self.requests.append(req)
        nxt = self.outcomes.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def test_post_failure_then_retry_uses_the_same_key(settings, db, catalogue, clock, dana):
    poster = FlakyPoster(
        [PostResult(False, error="unreachable", state={"token": "t1"}), PostResult(True, reference="R-1")]
    )
    service = DraftService(settings, db, catalogue, poster, clock)
    draft_id = new_draft(service)
    d = service.approve(draft_id, 1, dana)
    assert d["status"] == "post_failed" and d["last_error"] == "unreachable"
    d = service.approve(draft_id, d["version"], dana)
    assert d["status"] == "posted" and d["posted_ref"] == "R-1"
    first, second = poster.requests
    assert first.idempotency_key == second.idempotency_key
    assert second.state == {"token": "t1"}  # what the first attempt left is handed back
    assert events(db, draft_id) == ["extracted", "approved", "post_failed", "post_retried", "posted"]


def test_poster_crash_never_leaves_draft_posting(settings, db, catalogue, clock, dana):
    service = DraftService(settings, db, catalogue, FlakyPoster([RuntimeError("bug")]), clock)
    draft_id = new_draft(service)
    d = service.approve(draft_id, 1, dana)
    assert d["status"] == "post_failed" and d["last_error"] == "internal_error"


def test_stale_posting_claim_can_be_retried(settings, db, catalogue, clock, dana):
    service = DraftService(settings, db, catalogue, FlakyPoster([PostResult(True, reference="R-9")]), clock)
    draft_id = new_draft(service)
    with db.tx() as conn:  # simulate a crash right after the claim
        conn.execute(
            "UPDATE drafts SET status = 'posting', posting_started_at = '2026-11-09T09:00:00Z', post_key = 'k1', "
            "version = 2 WHERE id = ?",
            (draft_id,),
        )
    with pytest.raises(DraftError) as e:
        service.approve(draft_id, 2, dana)
    assert e.value.code == "status_posting"
    clock.advance(minutes=6)
    d = service.approve(draft_id, 2, dana)
    assert d["status"] == "posted" and d["post_key"] == "k1"


def test_post_failed_draft_can_be_rejected(settings, db, catalogue, clock, dana):
    service = DraftService(settings, db, catalogue, FlakyPoster([PostResult(False, error="price_mismatch")]), clock)
    draft_id = new_draft(service)
    d = service.approve(draft_id, 1, dana)
    service.reject(draft_id, d["version"], dana, "prices differ downstream")
    assert service.get(draft_id)["status"] == "rejected"


# --- edit and reject -----------------------------------------------------------------------------


def test_edit_reprices_and_audits(service, db, lee):
    draft_id = new_draft(service, kestrel(customer_email=None))
    service.edit(draft_id, 1, lee, 1, [("sku-00004", 10), ("SKU-00005", 3)])
    d = service.get(draft_id)
    assert d["version"] == 2 and d["customer_id"] == 1 and d["total"] == "110.10"
    trail = service.audit_trail(draft_id)
    assert trail[-1]["event"] == "edited" and trail[-1]["actor"] == "lee"
    assert trail[-1]["detail"]["after"]["lines"] == ["SKU-00004 x 10", "SKU-00005 x 3"]


def test_edit_without_changes_is_not_audited(service, db, lee):
    draft_id = new_draft(service)
    service.edit(draft_id, 1, lee, 1, [("SKU-00001", 24), ("SKU-00003", 10)])
    assert events(db, draft_id) == ["extracted"] and service.get(draft_id)["version"] == 1


@pytest.mark.parametrize(
    "customer, items, code",
    [
        (99, [("SKU-00001", 1)], "unknown_customer"),
        (1, [("SKU-99999", 1)], "unknown_product"),
        (1, [("SKU-00001", 0)], "bad_quantity"),
        (1, [("SKU-00001", 10_001)], "bad_quantity"),
        (1, [("SKU-00001", 1), ("sku-00001", 2)], "duplicate_product"),
    ],
)
def test_edit_validation(service, lee, customer, items, code):
    draft_id = new_draft(service)
    with pytest.raises(DraftError) as e:
        service.edit(draft_id, 1, lee, customer, items)
    assert e.value.code == code and e.value.status == 422


def test_edit_too_many_lines(service, settings, lee):
    service.s = settings.model_copy(update={"max_lines": 1})
    draft_id = new_draft(service)
    with pytest.raises(DraftError) as e:
        service.edit(draft_id, 1, lee, 1, [("SKU-00001", 1), ("SKU-00002", 1)])
    assert e.value.code == "too_many_lines"


def test_edit_only_while_pending(service, lee):
    draft_id = new_draft(service)
    service.reject(draft_id, 1, lee, "duplicate order")
    with pytest.raises(DraftError) as e:
        service.edit(draft_id, 2, lee, 1, [("SKU-00001", 1)])
    assert e.value.code == "status_rejected"


def test_reject_needs_a_reason(service, db, lee):
    draft_id = new_draft(service)
    with pytest.raises(DraftError):
        service.reject(draft_id, 1, lee, "  no ")
    service.reject(draft_id, 1, lee, "  Customer   cancelled by phone ")
    d = service.get(draft_id)
    assert (d["status"], d["decided_by"], d["reject_reason"]) == ("rejected", "lee", "Customer cancelled by phone")
    assert events(db, draft_id)[-1] == "rejected"


def test_price_rejects_unknown_product(service):
    with pytest.raises(DraftError):
        service.price([(999, 1)])


# --- append-only records -------------------------------------------------------------------------


def test_audit_log_is_append_only(service, db):
    new_draft(service)
    for sql in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with db.tx() as conn:
                conn.execute(sql)


def test_posted_invoices_are_immutable(service, db, dana):
    service.approve(new_draft(service), 1, dana)
    for sql in ("UPDATE invoices SET total = '0.01'", "DELETE FROM invoices"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with db.tx() as conn:
                conn.execute(sql)


def test_list_paginates_and_filters(service, settings, lee):
    service.s = settings.model_copy(update={"page_size": 5})
    ids = [new_draft(service, text=f"email {i}") for i in range(7)]
    service.reject(ids[0], 1, lee, "not needed")
    page1, more = service.list("pending", 1)
    page2, more2 = service.list("pending", 2)
    assert [r["id"] for r in page1] == ids[:0:-1][:5] and more
    assert [r["id"] for r in page2] == [ids[1]] and not more2
    rejected, _ = service.list("rejected", 1)
    assert [r["id"] for r in rejected] == [ids[0]]
    everything, _ = service.list(None, 1)
    assert len(everything) == 5 and everything[0]["customer"] == "Kestrel Kitchenware"

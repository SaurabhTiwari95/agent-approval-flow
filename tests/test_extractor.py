from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from approval import pricing
from approval.budget import SpendLedger
from approval.extractor import EXTRACTION_SCHEMA, FALLBACK_BETA, ClaudeExtractor, DemoExtractor
from tests.conftest import SAMPLES
from tests.fakes import FakeClaude, ok_response, response, text, usage

EMAIL = (SAMPLES / "order-001-kestrel.txt").read_text(encoding="utf-8")


def make(settings, db, catalogue, clock, script, cap="1.00", **overrides):
    s = settings.model_copy(update={"extract_day_cap_usd": Decimal(cap), **overrides})
    client = FakeClaude(script)
    return ClaudeExtractor(s, client, SpendLedger(db, s.extract_day_cap_usd, clock), catalogue), client


def _req():
    return httpx.Request("POST", "https://api.invalid/v1/messages")


def test_request_shape_follows_the_api_reference(settings, db, catalogue, clock):
    ex, client = make(settings, db, catalogue, clock, [ok_response()], extract_model="claude-opus-5-5")
    result = ex.extract("order-001.txt", EMAIL)
    assert result.status == "ok"
    endpoint, kw = client.calls[0]
    assert endpoint == "beta.messages"  # Opus 5.5: refusal fallbacks via the beta endpoint
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}}
    for banned in ("temperature", "thinking", "tool_choice", "tools"):
        assert banned not in kw
    # stable, cached prefix: system prompt + catalogue (no prices), breakpoint on the last block
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "SKU-00001\tSteel Mixing Bowl 3L" in kw["system"][-1]["text"]
    assert "12.50" not in kw["system"][-1]["text"]
    assert kw["messages"][0]["content"].startswith('<email source="order-001.txt">')


def test_system_prefix_is_byte_identical_across_emails(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [])
    assert ex.request("a.txt", "one")["system"] == ex.request("b.txt", "two")["system"]


def test_source_name_is_sanitised_in_the_prompt(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [])
    content = ex.request('evil"><x>.txt', "hi")["messages"][0]["content"]
    assert content.startswith('<email source="evil___x_.txt">')


def test_schema_is_strict_everywhere():
    assert EXTRACTION_SCHEMA["additionalProperties"] is False
    item = EXTRACTION_SCHEMA["properties"]["lines"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert set(EXTRACTION_SCHEMA["required"]) == set(EXTRACTION_SCHEMA["properties"])
    assert "price" not in json.dumps(EXTRACTION_SCHEMA)  # the agent has nowhere to put a price


def test_default_model_is_haiku_on_the_plain_endpoint(settings, db, catalogue, clock):
    ex, client = make(settings, db, catalogue, clock, [ok_response()])
    assert ex.extract("a.txt", EMAIL).status == "ok"
    name, kw = client.calls[0]
    assert name == "messages" and "fallbacks" not in kw and "betas" not in kw
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}}


def test_cost_recorded_from_usage(settings, db, catalogue, clock):
    ex, _ = make(
        settings, db, catalogue, clock, [response([text(ok_response().content[1].text)], use=usage(1000, 500))]
    )
    result = ex.extract("a.txt", EMAIL)
    expected = pricing.cost(pricing.Usage(1000, 500), "claude-haiku-5-5")
    assert result.usd == expected == Decimal("0.000350")
    assert ex.ledger.committed() == expected
    row = db.one("SELECT status, input_tokens, output_tokens FROM extraction_costs")
    assert (row["status"], row["input_tokens"], row["output_tokens"]) == ("settled", 1000, 500)


def test_fallback_model_is_priced_at_its_own_rates(settings, db, catalogue, clock):
    ex, _ = make(
        settings,
        db,
        catalogue,
        clock,
        [response([text(ok_response().content[1].text)], model="claude-opus-5", use=usage(1000, 0))],
    )
    result = ex.extract("a.txt", EMAIL)
    assert result.model == "claude-opus-5" and result.usd == Decimal("0.005000")


def test_refusal_checked_before_content(settings, db, catalogue, clock):
    refused = response([], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    ex, _ = make(settings, db, catalogue, clock, [refused])
    result = ex.extract("a.txt", EMAIL)
    assert result.status == "refused" and "cyber" in result.detail and result.extraction is None


def test_output_limit(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [response([text('{"cust')], stop_reason="max_tokens")])
    assert ex.extract("a.txt", EMAIL).status == "output_limit"


@pytest.mark.parametrize("bad", ["not json", '{"customer_name": 1}', json.dumps({"lines": [{"quantity": "many"}]})])
def test_invalid_output_is_caught(settings, db, catalogue, clock, bad):
    ex, _ = make(settings, db, catalogue, clock, [response([text(bad)])])
    assert ex.extract("a.txt", EMAIL).status == "invalid_output"


def test_api_status_error_is_not_billed(settings, db, catalogue, clock):
    err = anthropic.APIStatusError("overloaded", response=httpx.Response(529, request=_req()), body=None)
    ex, _ = make(settings, db, catalogue, clock, [err])
    result = ex.extract("a.txt", EMAIL)
    assert result.status == "api_error" and "529" in result.detail
    assert ex.ledger.committed() == 0


def test_transport_error_counts_the_worst_case(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [anthropic.APIConnectionError(request=_req())])
    result = ex.extract("a.txt", EMAIL)
    assert result.status == "api_error" and result.usd > 0
    assert ex.ledger.committed() == result.usd


def test_client_error_before_sending(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [TypeError("no credentials")])
    result = ex.extract("a.txt", EMAIL)
    assert result.status == "client_error" and ex.ledger.committed() == 0


def test_unexpected_exception_keeps_worst_case_and_raises(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        ex.extract("a.txt", EMAIL)
    assert ex.ledger.committed() > 0
    assert db.one("SELECT status FROM extraction_costs")["status"] == "settled"


def test_day_cap_stops_before_calling(settings, db, catalogue, clock):
    ex, client = make(settings, db, catalogue, clock, [ok_response()], cap="0.001")
    result = ex.extract("a.txt", EMAIL)
    assert result.status == "day_cap" and "cap" in result.detail
    assert client.calls == []


def test_day_cap_counts_earlier_spend_and_resets_next_day(settings, db, catalogue, clock):
    probe, _ = make(settings, db, catalogue, clock, [])
    kw = probe.request("a.txt", EMAIL)
    worst = pricing.worst_case_cost(
        "claude-haiku-5-5", len(json.dumps(kw, ensure_ascii=False)) // 3 + 1, kw["max_tokens"]
    )
    spent = pricing.cost(pricing.Usage.from_api(ok_response().usage), "claude-haiku-5-5")
    assert spent > 0
    cap = str(worst + spent - Decimal("0.000001"))  # room for one call's worst case, not for that plus real spend
    ex, client = make(settings, db, catalogue, clock, [ok_response(), ok_response()], cap=cap)
    assert ex.extract("a.txt", EMAIL).status == "ok"
    assert ex.extract("a.txt", EMAIL).status == "day_cap"
    clock.advance(days=1)
    assert ex.extract("a.txt", EMAIL).status == "ok"
    assert len(client.calls) == 2


def test_cost_report(settings, db, catalogue, clock):
    ex, _ = make(settings, db, catalogue, clock, [ok_response()])
    ex.extract("a.txt", EMAIL)
    [row] = ex.ledger.report(7)
    assert row["model"] == "claude-haiku-5-5" and row["calls"] == 1 and row["usd"] > 0


def test_pricing_unknown_model_uses_worst_rates():
    assert pricing.price_for("some-future-model").output == max(p.output for p in pricing.PRICES.values())
    assert pricing.worst_price("claude-opus-5-5").input == Decimal("5.00")  # a fallback could be dearer
    assert pricing.from_micros(pricing.to_micros(Decimal("0.123456"))) == Decimal("0.123456")


def test_haiku_long_prompts_are_priced_at_the_higher_rate():
    short, long = pricing.Usage(100_000, 1000), pricing.Usage(100_001, 1000)
    assert pricing.cost(short, "claude-haiku-5-5") == Decimal("0.010500")
    assert pricing.cost(long, "claude-haiku-5-5") == Decimal("0.052500")  # 0.0525005 rounds half-even
    # cached tokens count toward the prompt size
    assert pricing.price_for("claude-haiku-5-5", 100_001).input == Decimal("0.50")
    assert pricing.cost(pricing.Usage(2, 0, 0, 100_000), "claude-haiku-5-5") == Decimal("0.005001")
    assert (
        pricing.worst_case_cost("claude-haiku-5-5", 200_000, 0)
        > pricing.worst_case_cost("claude-haiku-5-5", 100_000, 0) * 2
    )


# --- demo extractor ------------------------------------------------------------------------------


def test_demo_extractor_reads_coded_lines():
    result = DemoExtractor().extract("a.txt", EMAIL)
    ex = result.extraction
    assert result.status == "ok" and result.usd == 0
    assert ex.customer_email == "purchasing@kestrel.example.com"
    assert [(ln.sku, ln.quantity) for ln in ex.lines] == [("SKU-00001", 24), ("SKU-00003", 10), ("SKU-00010", 60)]
    assert ex.lines[0].product_name == "Steel Mixing Bowl 3L"
    assert ex.requested_date == "2026-11-16"


@pytest.mark.parametrize(
    "line, qty",
    [
        ("SKU-00012 Copper Kettle 1.7L x 1200", 1200),
        ("SKU-00007 Stainless Cutlery Set (24 pc) x 200", 200),
        ("- 40 x SKU-00006 (glass storage jars)", 40),
        ("SKU-00009 Enamel Mug - 48", 48),
        ("SKU-00008: 6", 6),
    ],
)
def test_demo_extractor_quantities(line, qty):
    [ln] = DemoExtractor().extract("a.txt", line).extraction.lines
    assert ln.quantity == qty


def test_demo_extractor_misses_uncoded_lines():
    ex = DemoExtractor().extract("a.txt", (SAMPLES / "order-002-marigold.txt").read_text(encoding="utf-8")).extraction
    assert ex.lines == [] and ex.customer_email == "orders@marigoldfinch.example.com"

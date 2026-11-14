"""Stand-ins for the paid and remote things. No network, no cost: every request is recorded so tests can
assert exactly what would have been sent."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx

from approval import signing


def usage(inp=1_200, out=300, cache_write=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out, cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read
    )


def text(t):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def response(content, stop_reason="end_turn", model="claude-haiku-5-5", use=None, stop_details=None):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model, usage=use or usage(), stop_details=stop_details
    )


def extraction_json(**overrides) -> str:
    data = {
        "customer_name": "Kestrel Kitchenware",
        "customer_email": "purchasing@kestrel.example.com",
        "lines": [
            {"sku": "SKU-00001", "sku_source": "email", "product_name": "Steel Mixing Bowl 3L", "quantity": 24},
            {"sku": "SKU-00003", "sku_source": "email", "product_name": "Cast Iron Skillet 26cm", "quantity": 10},
        ],
        "requested_date": "2026-11-16",
        "confidence": "high",
        "notes": [],
    }
    data.update(overrides)
    return json.dumps(data)


def ok_response(**overrides):
    return response([thinking(), text(extraction_json(**overrides))])


class _Endpoint:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def create(self, **kwargs):
        self.owner.calls.append((self.name, kwargs))
        if not self.owner.script:
            raise AssertionError(f"unexpected extra call to {self.name}")
        nxt = self.owner.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt(kwargs) if callable(nxt) else nxt


class FakeClaude:
    """Same surface as anthropic.Anthropic for what this project uses."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, dict]] = []
        self.messages = _Endpoint(self, "messages")
        self.beta = SimpleNamespace(messages=_Endpoint(self, "beta.messages"))


class Clock:
    def __init__(self, start=dt.datetime(2026, 11, 9, 9, 0, tzinfo=dt.UTC)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


class FakeWriteApi:
    """The governed-write-api HTTP contract, in memory: signed requests, validate -> token -> commit,
    idempotency keys bound to the request body, stored answers replayed."""

    def __init__(self, key_id: str, secret: str, prices: dict[int, str]):
        self.key_id, self.secret, self.prices = key_id, secret, prices
        self.tokens: dict[str, dict] = {}
        self.idem: dict[str, tuple[str, int, dict]] = {}
        self.invoices: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.fail_next: list = []  # status codes / exceptions to return before handling

    def _json(self, status, body, replayed=False):
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return httpx.Response(status, json=body, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = request.content
        h = request.headers
        ok = h.get("X-Key-Id") == self.key_id and signing.verify(
            self.secret,
            h.get("X-Signature", ""),
            request.method,
            request.url.path,
            h["X-Timestamp"],
            h["X-Nonce"],
            body,
        )
        if not ok:
            return self._json(401, {"error": "unauthorized"})
        if self.fail_next:
            nxt = self.fail_next.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return self._json(nxt[0], {"error": nxt[1]})
        payload = json.loads(body)
        if request.url.path == "/v1/validate/create_invoice":
            p = payload["params"]
            lines, total = [], 0
            for ln in p["lines"]:
                price = self.prices[ln["product_id"]]
                lines.append({**ln, "unit_price": price})
                total += round(float(price) * 100) * ln["quantity"]
            preview = {"customer_id": p["customer_id"], "lines": lines, "total": f"{total / 100:.2f}"}
            token = f"tok{len(self.tokens) + 1:04d}"
            self.tokens[token] = {"preview": preview, "used": False}
            return self._json(200, {"action": "create_invoice", "token": token, "preview": preview})
        if request.url.path == "/v1/commit":
            key = h.get("Idempotency-Key")
            if key in self.idem:
                digest, status, stored = self.idem[key]
                if digest != body.decode():
                    return self._json(422, {"error": "idempotency_key_reused"})
                return self._json(status, stored, replayed=True)
            tok = self.tokens.get(payload["token"])
            if tok is None:
                status, out = 404, {"error": "unknown_token"}
            elif tok["used"]:
                status, out = 409, {"error": "token_already_used"}
            else:
                tok["used"] = True
                self.invoices.append(tok["preview"])
                status = 201
                out = {
                    "action": "create_invoice",
                    "result": {"invoice_id": 6000 + len(self.invoices), "total": tok["preview"]["total"]},
                }
            self.idem[key] = (body.decode(), status, out)
            return self._json(status, out)
        return self._json(404, {"error": "not_found"})

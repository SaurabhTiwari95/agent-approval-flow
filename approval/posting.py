"""Where an approved invoice is posted. Pluggable (POST_TARGET):

  local               this app's own `invoices` table (one invoice per draft, enforced by the schema)
  governed-write-api  my governed-write-api service: validate -> compare totals -> commit, with an
                      Idempotency-Key, over HMAC-signed requests

Both are safe to call again for the same draft: a retry never creates a second invoice.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import httpx

from approval import signing
from approval.db import Database, iso, utc_now


@dataclass(frozen=True)
class PostRequest:
    draft_id: int
    idempotency_key: str
    customer_id: int
    lines: list[dict[str, Any]]  # product_id, sku, name, quantity, unit_price, line_total
    total: Decimal
    issued_on: dt.date
    approver: str
    approver_name: str
    drafted_by: str
    state: dict[str, Any] = field(default_factory=dict)  # what an earlier attempt left (for safe retries)


@dataclass
class PostResult:
    ok: bool
    reference: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


class Poster(Protocol):
    target: str

    def post(self, req: PostRequest) -> PostResult: ...


# --- local ---------------------------------------------------------------------------------------


class LocalPoster:
    target = "local"

    def __init__(self, db: Database, clock=utc_now):
        self.db, self.clock = db, clock

    def post(self, req: PostRequest) -> PostResult:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO invoices (draft_id, idempotency_key, customer_id, lines_json, total, issued_on, "
                "posted_by, posted_by_name, drafted_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (draft_id) DO NOTHING",
                (
                    req.draft_id,
                    req.idempotency_key,
                    req.customer_id,
                    json.dumps(req.lines),
                    str(req.total),
                    req.issued_on.isoformat(),
                    req.approver,
                    req.approver_name,
                    req.drafted_by,
                    iso(self.clock()),
                ),
            )
            row = conn.execute("SELECT id, total FROM invoices WHERE draft_id = ?", (req.draft_id,)).fetchone()
        return PostResult(True, reference=f"INV-{row['id']:06d}", detail={"total": row["total"]})


# --- governed-write-api --------------------------------------------------------------------------


class WriteApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"{status} {body.get('error', 'error')}")
        self.status, self.body = status, body

    @property
    def code(self) -> str:
        return str(self.body.get("error", "error"))[:60]


class WriteApiClient:
    """A small signed client for governed-write-api (same contract as that project's own client)."""

    def __init__(self, base_url: str, key_id: str, secret: str, timeout: float = 10.0, **client_kwargs):
        self._key_id, self._secret = key_id, secret
        self._http = httpx.Client(base_url=base_url, timeout=timeout, **client_kwargs)

    def _send(self, path: str, payload: dict[str, Any], idem_key: str | None = None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        headers = signing.signed_headers(self._key_id, self._secret, "POST", path, body)
        headers["Content-Type"] = "application/json"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        response = self._http.post(path, content=body, headers=headers)
        try:
            data = response.json()
        except ValueError:
            data = {"error": "non_json_response"}
        if response.status_code >= 400:
            raise WriteApiError(response.status_code, data if isinstance(data, dict) else {})
        data["_replayed"] = response.headers.get("Idempotent-Replayed") == "true"
        return data

    def validate(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._send(f"/v1/validate/{action}", {"params": params})

    def commit(self, token: str, idempotency_key: str) -> dict[str, Any]:
        return self._send("/v1/commit", {"token": token}, idempotency_key)

    def close(self) -> None:
        self._http.close()


# Answers to a commit that mean "this token will never write": start again with a new validation.
_TOKEN_DEAD = {"unknown_token", "token_expired", "state_changed", "rule_violation", "invalid_params", "invalid_request"}


class GovernedWriteApiPoster:
    target = "governed-write-api"

    def __init__(self, client: WriteApiClient, due_in_days: int = 30):
        self.client, self.due_in_days = client, due_in_days

    def _fail(self, error: str, state: dict[str, Any], **detail) -> PostResult:
        return PostResult(False, error=error, detail=detail, state=state)

    def _committed(self, res: dict[str, Any], state: dict[str, Any]) -> PostResult:
        result = res.get("result", {})
        return PostResult(
            True,
            reference=f"writeapi-invoice-{result.get('invoice_id')}",
            detail={"total": result.get("total"), "replayed": res.get("_replayed", False)},
            state={**state, "done": True},
        )

    def _commit(self, state: dict[str, Any]) -> PostResult:
        try:
            return self._committed(self.client.commit(state["token"], state["key"]), state)
        except WriteApiError as exc:
            if exc.code in _TOKEN_DEAD:  # nothing was written; the next attempt validates afresh
                return self._fail(exc.code, {"attempt": state["attempt"]}, status=exc.status)
            return self._fail(exc.code, state, status=exc.status)  # temporary: retry re-sends the same commit
        except httpx.HTTPError as exc:  # maybe written, maybe not: the same key + token makes the retry safe
            return self._fail("unreachable", state, reason=type(exc).__name__)

    def post(self, req: PostRequest) -> PostResult:
        state = dict(req.state)
        if state.get("token") and state.get("key"):
            return self._commit(state)

        attempt = int(state.get("attempt", 0)) + 1
        key = req.idempotency_key if attempt == 1 else f"{req.idempotency_key}-r{attempt}"
        params = {
            "customer_id": req.customer_id,
            "issued_on": req.issued_on.isoformat(),
            "due_in_days": self.due_in_days,
            "lines": [{"product_id": ln["product_id"], "quantity": ln["quantity"]} for ln in req.lines],
        }
        try:
            check = self.client.validate("create_invoice", params)
        except WriteApiError as exc:
            return self._fail(exc.code, {"attempt": attempt}, status=exc.status, details=exc.body.get("details"))
        except httpx.HTTPError as exc:
            return self._fail("unreachable", {"attempt": attempt}, reason=type(exc).__name__)

        remote_total = Decimal(str(check.get("preview", {}).get("total", "-1")))
        if remote_total != req.total:  # the person approved one amount; never post a different one
            return self._fail("price_mismatch", {"attempt": attempt}, approved=str(req.total), remote=str(remote_total))
        return self._commit({"attempt": attempt, "token": check["token"], "key": key})


def new_post_key() -> str:
    return uuid.uuid4().hex

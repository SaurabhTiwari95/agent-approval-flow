"""Draft invoices: from an email, through a person's decision, to a posted invoice.

    pending ──approve──> posting ──ok──> posted
       │                    └──fail──> post_failed ──approve (retry, same key)──> posting
       ├──reject──> rejected            (post_failed can also be rejected)
       └──48 h────> expired             (pending and post_failed both expire)

Rules enforced here, at the moment of each action (never trusted from the page):
  - only an approver (not a viewer) may edit, approve or reject;
  - an approval names the version the person saw; if anything changed since, it is refused;
  - a draft past its expiry can't be approved; it is marked expired instead;
  - the total must be within MAX_DRAFT_TOTAL and the approver's own limit;
  - the claim (pending -> posting) is one IMMEDIATE transaction, so two approvals of one draft,
    at the same moment or one after the other, post once;
  - every step is written to the append-only audit log.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from approval.catalogue import Catalogue, money
from approval.config import Settings
from approval.db import Database, audit, iso, loads, parse_iso, utc_now
from approval.extractor import Extraction, ExtractResult
from approval.posting import Poster, PostRequest, PostResult, new_post_key

log = logging.getLogger("approval")

STALE_POSTING = dt.timedelta(minutes=5)  # a claim older than this (crashed mid-post) may be retried
STATUSES = ("pending", "posting", "posted", "post_failed", "rejected", "expired")
OPEN = ("pending", "post_failed")


class DraftError(Exception):
    """A refused action. `status` is the HTTP status the web layer should use."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class User:
    username: str
    name: str
    role: str
    limit: Decimal | None = None

    @property
    def can_decide(self) -> bool:
        return self.role == "approver"


@dataclass
class IngestOutcome:
    source: str
    status: str  # created | duplicate | skipped | too_large
    draft_id: int | None = None
    usd: Decimal = Decimal("0")
    detail: str = ""
    lines: int = 0
    total: str = "0.00"
    problems: list[str] = field(default_factory=list)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DraftService:
    def __init__(self, settings: Settings, db: Database, catalogue: Catalogue, poster: Poster, clock=utc_now):
        self.s, self.db, self.catalogue, self.poster, self.clock = settings, db, catalogue, poster, clock

    # --- pricing (the server's job, never the agent's) ---------------------------------------------

    def price(self, items: list[tuple[int, int]]) -> tuple[list[dict[str, Any]], Decimal]:
        lines, total = [], Decimal("0")
        for product_id, quantity in items:
            p = self.catalogue.product(product_id)
            if p is None:
                raise DraftError("unknown_product", f"product {product_id} is not in the price list", 422)
            line_total = p.unit_price * quantity
            total += line_total
            lines.append(
                {
                    "product_id": p.id,
                    "sku": p.sku,
                    "name": p.name,
                    "quantity": quantity,
                    "unit_price": money(p.unit_price),
                    "line_total": money(line_total),
                }
            )
        return lines, Decimal(money(total))

    def _from_extraction(self, ex: Extraction) -> tuple[int | None, list[tuple[int, int]], list[dict], str | None]:
        """Match the agent's proposal to real customers and products; everything unmatched becomes a
        problem for the approver, never a guess."""
        problems: list[dict[str, str]] = []
        customer = self.catalogue.match_customer(ex.customer_email, ex.customer_name)
        if customer is None:
            who = ex.customer_email or ex.customer_name or "no sender found"
            problems.append({"code": "unknown_customer", "message": f"Customer not recognised ({who[:120]})"})
        items: dict[int, int] = {}
        for line in ex.lines[: self.s.max_lines]:
            product = (self.catalogue.product_by_sku(line.sku) if line.sku else None) or self.catalogue.product_by_name(
                line.product_name
            )
            label = (line.sku or line.product_name)[:120]
            if product is None:
                problems.append({"code": "unknown_product", "message": f"Not in the price list: {label}"})
                continue
            if not 1 <= line.quantity <= self.s.max_quantity:
                problems.append({"code": "bad_quantity", "message": f"Quantity {line.quantity} for {label}"})
                continue
            if line.sku_source == "catalogue_match":
                problems.append(
                    {"code": "inferred_sku", "message": f"'{line.product_name[:80]}' matched to {product.sku}: check"}
                )
            items[product.id] = items.get(product.id, 0) + line.quantity  # same product twice: one line
        if not ex.lines:
            problems.append({"code": "no_lines", "message": "The agent found no order lines: add them by hand"})
        if len(ex.lines) > self.s.max_lines:
            problems.append({"code": "too_many_lines", "message": f"Only the first {self.s.max_lines} lines were read"})
        requested = ex.requested_date
        if requested is not None:
            try:
                requested = dt.date.fromisoformat(requested).isoformat()
            except ValueError:
                problems.append({"code": "bad_date", "message": f"Requested date not understood: {requested[:20]}"})
                requested = None
        return (customer.id if customer else None), list(items.items()), problems, requested

    # --- ingestion ---------------------------------------------------------------------------------

    def ingest(self, source_name: str, email_text: str, extractor) -> IngestOutcome:
        source_name = source_name[:200]
        if len(email_text) > self.s.max_email_chars:
            return IngestOutcome(source_name, "too_large", detail=f"over {self.s.max_email_chars} characters")
        digest = sha256(email_text)
        existing = self.db.one("SELECT id FROM drafts WHERE source_sha256 = ?", (digest,))
        if existing:  # never pay twice for the same email
            return IngestOutcome(source_name, "duplicate", existing["id"])

        result: ExtractResult = extractor.extract(source_name, email_text)
        if result.status == "day_cap":
            return IngestOutcome(source_name, "skipped", detail=result.detail)

        if result.status == "ok":
            ex = result.extraction
            customer_id, items, problems, requested = self._from_extraction(ex)
            confidence, notes, raw = ex.confidence, ex.clipped_notes(), ex.model_dump()
        else:  # still a draft: a person should see every order, even one the agent couldn't read
            customer_id, items, requested, confidence, notes, raw = None, [], None, None, [], {}
            problems = [
                {"code": f"extraction_{result.status}", "message": f"The agent couldn't read it: {result.detail}"}
            ]
        lines, total = self.price(items)

        now = self.clock()
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM drafts WHERE source_sha256 = ?", (digest,)).fetchone():
                return IngestOutcome(source_name, "duplicate")  # another process got there first
            cur = conn.execute(
                "INSERT INTO drafts (source_name, source_sha256, email_text, customer_id, requested_date, lines_json, "
                "total, extraction_json, confidence, notes_json, problems_json, extracted_by, extraction_usd, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_name,
                    digest,
                    email_text,
                    customer_id,
                    requested,
                    json.dumps(lines),
                    money(total),
                    json.dumps(raw),
                    confidence,
                    json.dumps(notes),
                    json.dumps(problems),
                    result.model,
                    str(result.usd),
                    iso(now),
                    iso(now + dt.timedelta(hours=self.s.draft_ttl_hours)),
                ),
            )
            draft_id = cur.lastrowid
            event = "extracted" if result.status == "ok" else "extraction_failed"
            audit(
                conn,
                now,
                draft_id,
                event,
                f"agent:{result.model}",
                source=source_name,
                status=result.status,
                usd=str(result.usd),
                lines=len(lines),
                total=money(total),
                problems=[p["code"] for p in problems],
            )
        return IngestOutcome(
            source_name,
            "created",
            draft_id,
            result.usd,
            result.status,
            len(lines),
            money(total),
            [p["code"] for p in problems],
        )

    # --- reading -----------------------------------------------------------------------------------

    def sweep_expired(self, draft_id: int | None = None) -> int:
        """Mark open drafts past their expiry as expired (and audit each). Cheap: uses a partial index.
        Runs before every action and list, and from `python -m approval expire` (for a cron job)."""
        now = self.clock()
        sql = (
            "UPDATE drafts SET status = 'expired', version = version + 1 "
            "WHERE status IN ('pending', 'post_failed') AND expires_at <= ?"
        )
        params: tuple = (iso(now),)
        if draft_id is not None:
            sql += " AND id = ?"
            params = (iso(now), draft_id)
        # Read first (partial index, no lock): most calls find nothing and never take the write lock.
        probe = "SELECT 1 FROM drafts WHERE status IN ('pending', 'post_failed') AND expires_at <= ?"
        if not self.db.one(probe + (" AND id = ?" if draft_id is not None else "") + " LIMIT 1", params):
            return 0
        with self.db.tx() as conn:
            rows = conn.execute(sql + " RETURNING id, expires_at", params).fetchall()
            for r in rows:
                audit(conn, now, r["id"], "expired", "system", expires_at=r["expires_at"])
        return len(rows)

    def list(self, status: str | None, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
        """One query per page (no N+1): the total and line count are stored on the draft."""
        size = self.s.page_size
        page = max(1, min(page, 10_000))
        sql = (
            "SELECT d.id, d.source_name, d.status, d.total, d.created_at, d.expires_at, d.confidence, "
            "d.lines_json, d.problems_json, d.decided_by, d.posted_ref, c.name AS customer "
            "FROM drafts d LEFT JOIN customers c ON c.id = d.customer_id "
        )
        params: tuple = ()
        if status in STATUSES:
            sql += "WHERE d.status = ? "
            params = (status,)
        sql += "ORDER BY d.id DESC LIMIT ? OFFSET ?"
        rows = self.db.all(sql, (*params, size + 1, (page - 1) * size))
        out = []
        for r in rows[:size]:
            out.append(
                {
                    "id": r["id"],
                    "source_name": r["source_name"],
                    "status": r["status"],
                    "total": r["total"],
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "confidence": r["confidence"],
                    "lines": len(loads(r["lines_json"], [])),
                    "problems": len(loads(r["problems_json"], [])),
                    "customer": r["customer"],
                    "decided_by": r["decided_by"],
                    "posted_ref": r["posted_ref"],
                }
            )
        return out, len(rows) > size

    def get(self, draft_id: int) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        if r is None:
            raise DraftError("not_found", "No such draft", 404)
        d = dict(r)
        d["lines"] = loads(d.pop("lines_json"), [])
        d["notes"] = loads(d.pop("notes_json"), [])
        d["problems"] = loads(d.pop("problems_json"), [])
        d["extraction"] = loads(d.pop("extraction_json"), {})
        d["post_state"] = loads(d.pop("post_state_json"), {})
        d["customer"] = self.catalogue.customer(d["customer_id"])
        return d

    def audit_trail(self, draft_id: int) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT at, event, actor, detail_json FROM audit_log WHERE draft_id = ? ORDER BY id LIMIT 500", (draft_id,)
        )
        return [
            {"at": r["at"], "event": r["event"], "actor": r["actor"], "detail": loads(r["detail_json"], {})}
            for r in rows
        ]

    def blockers(self, d: dict[str, Any], user: User | None = None) -> list[str]:
        """Why this draft can't be approved right now (empty list: it can)."""
        out = []
        if d["customer_id"] is None:
            out.append("Choose the customer.")
        if not d["lines"]:
            out.append("Add at least one line.")
        total = Decimal(d["total"])
        if total > self.s.max_draft_total:
            out.append(f"Total {money(total)} is over the per-draft ceiling of {money(self.s.max_draft_total)}.")
        if user and user.limit is not None and total > user.limit:
            out.append(f"Total {money(total)} is over your approval limit of {money(user.limit)}.")
        return out

    # --- actions -----------------------------------------------------------------------------------

    def _load_for_action(self, conn, draft_id: int, version: int, user: User, allowed: tuple[str, ...]):
        if not user.can_decide:
            raise DraftError("forbidden", "Viewers can't change drafts.", 403)
        r = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if r is None:
            raise DraftError("not_found", "No such draft", 404)
        now = self.clock()
        if r["status"] == "expired" or (r["status"] in OPEN and parse_iso(r["expires_at"]) <= now):
            raise DraftError("expired", "This draft has expired and can no longer be approved.", 410)
        if r["status"] not in allowed:
            raise DraftError(f"status_{r['status']}", f"This draft is {r['status'].replace('_', ' ')}.")
        if r["version"] != version:
            raise DraftError("changed", "The draft changed since you opened it. Review it again.")
        return r, now

    def edit(
        self, draft_id: int, version: int, user: User, customer_id: int | None, items: list[tuple[str, int]]
    ) -> None:
        if customer_id is not None and self.catalogue.customer(customer_id) is None:
            raise DraftError("unknown_customer", "No such customer.", 422)
        merged: dict[int, int] = {}
        for sku, qty in items:
            p = self.catalogue.product_by_sku(sku)
            if p is None:
                raise DraftError("unknown_product", f"{sku[:40]} is not in the price list.", 422)
            if not 1 <= qty <= self.s.max_quantity:
                raise DraftError("bad_quantity", f"Quantity for {p.sku} must be 1 to {self.s.max_quantity}.", 422)
            if p.id in merged:
                raise DraftError("duplicate_product", f"{p.sku} appears twice; use one line.", 422)
            merged[p.id] = qty
        if len(merged) > self.s.max_lines:
            raise DraftError("too_many_lines", f"At most {self.s.max_lines} lines.", 422)
        lines, total = self.price(list(merged.items()))
        self.sweep_expired(draft_id)
        with self.db.tx() as conn:
            r, now = self._load_for_action(conn, draft_id, version, user, ("pending",))
            before = {
                "customer_id": r["customer_id"],
                "lines": _summary(loads(r["lines_json"], [])),
                "total": r["total"],
            }
            after = {"customer_id": customer_id, "lines": _summary(lines), "total": money(total)}
            if before == after:
                return
            conn.execute(
                "UPDATE drafts SET customer_id = ?, lines_json = ?, total = ?, version = version + 1 WHERE id = ?",
                (customer_id, json.dumps(lines), money(total), draft_id),
            )
            audit(conn, now, draft_id, "edited", user.username, before=before, after=after)

    def reject(self, draft_id: int, version: int, user: User, reason: str) -> None:
        reason = " ".join(reason.split())
        if not 3 <= len(reason) <= 500:
            raise DraftError("reason_required", "Give a reason (3 to 500 characters).", 422)
        self.sweep_expired(draft_id)
        with self.db.tx() as conn:
            _, now = self._load_for_action(conn, draft_id, version, user, OPEN)
            conn.execute(
                "UPDATE drafts SET status = 'rejected', decided_by = ?, decided_at = ?, reject_reason = ?, "
                "version = version + 1 WHERE id = ?",
                (user.username, iso(now), reason, draft_id),
            )
            audit(conn, now, draft_id, "rejected", user.username, reason=reason)

    def approve(self, draft_id: int, version: int, user: User) -> dict[str, Any]:
        """Claim the draft, post it under the approver's name, record the outcome. Returns the draft."""
        self.sweep_expired(draft_id)
        with self.db.tx() as conn:
            now = self.clock()
            r = conn.execute("SELECT status, posting_started_at FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            allowed = OPEN
            if r and r["status"] == "posting" and parse_iso(r["posting_started_at"]) <= now - STALE_POSTING:
                allowed = (*OPEN, "posting")  # the earlier attempt crashed; the same key makes a retry safe
            r, now = self._load_for_action(conn, draft_id, version, user, allowed)
            d = self.get(draft_id)
            problems = self.blockers(d, user)
            if problems:  # recorded (the transaction commits), then refused below
                audit(conn, now, draft_id, "approve_refused", user.username, reasons=problems)
            else:
                key = r["post_key"] or new_post_key()
                self._claim(conn, now, d, r["status"], key, user)
        if problems:
            raise DraftError("not_approvable", " ".join(problems), 422)
        return self._post(d, key, now, user)

    def _claim(self, conn, now: dt.datetime, d: dict[str, Any], status: str, key: str, user: User) -> None:
        conn.execute(
            "UPDATE drafts SET status = 'posting', decided_by = ?, decided_at = ?, post_key = ?, "
            "posting_started_at = ?, version = version + 1 WHERE id = ?",
            (user.username, iso(now), key, iso(now), d["id"]),
        )
        audit(
            conn,
            now,
            d["id"],
            "approved" if status == "pending" else "post_retried",
            user.username,
            total=d["total"],
            lines=_summary(d["lines"]),
            target=self.poster.target,
        )

    def _post(self, d: dict[str, Any], key: str, now: dt.datetime, user: User) -> dict[str, Any]:
        draft_id = d["id"]
        req = PostRequest(
            draft_id=draft_id,
            idempotency_key=key,
            customer_id=d["customer_id"],
            lines=d["lines"],
            total=Decimal(d["total"]),
            issued_on=now.date(),
            approver=user.username,
            approver_name=user.name,
            drafted_by=d["extracted_by"],
            state=d["post_state"],
        )
        try:
            result = self.poster.post(req)
        except Exception as exc:  # never leave a draft stuck in "posting" because of a bug downstream
            log.exception("posting draft %s failed", draft_id)
            result = PostResult(False, error="internal_error", detail={"type": type(exc).__name__}, state=req.state)

        done = self.clock()
        with self.db.tx() as conn:
            if result.ok:
                conn.execute(
                    "UPDATE drafts SET status = 'posted', posted_ref = ?, posted_at = ?, post_state_json = ?, "
                    "last_error = NULL, version = version + 1 WHERE id = ?",
                    (result.reference, iso(done), json.dumps(result.state), draft_id),
                )
                event, detail = "posted", {"reference": result.reference, "posted_by": user.username}
            else:
                conn.execute(
                    "UPDATE drafts SET status = 'post_failed', last_error = ?, post_state_json = ?, "
                    "version = version + 1 WHERE id = ?",
                    (result.error, json.dumps(result.state), draft_id),
                )
                event, detail = "post_failed", {"error": result.error}
            audit(conn, done, draft_id, event, user.username, target=self.poster.target, **detail, **result.detail)
        return self.get(draft_id)


def _summary(lines: list[dict[str, Any]]) -> list[str]:
    return [f"{ln['sku']} x {ln['quantity']}" for ln in lines]

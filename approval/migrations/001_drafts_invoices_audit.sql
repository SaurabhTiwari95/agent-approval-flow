-- Business data, drafts, posted invoices and the append-only audit log.
-- Money is stored as exact decimal text ("1234.50") and computed with Python's Decimal.
-- Times are UTC ISO-8601 text ("2026-11-09T10:15:00Z"), so text comparison orders them.

CREATE TABLE customers (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE INDEX customers_name ON customers (name COLLATE NOCASE);

CREATE TABLE products (
    id         INTEGER PRIMARY KEY,
    sku        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name       TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE INDEX products_name ON products (name COLLATE NOCASE);

CREATE TABLE drafts (
    id                 INTEGER PRIMARY KEY,
    source_name        TEXT NOT NULL,
    source_sha256      TEXT NOT NULL UNIQUE,      -- the same email is never drafted (or paid for) twice
    email_text         TEXT NOT NULL,
    customer_id        INTEGER REFERENCES customers (id),
    requested_date     TEXT,
    lines_json         TEXT NOT NULL DEFAULT '[]', -- priced by the server, never by the agent
    total              TEXT NOT NULL DEFAULT '0.00',
    extraction_json    TEXT NOT NULL,              -- exactly what the agent returned (after validation)
    confidence         TEXT,
    notes_json         TEXT NOT NULL DEFAULT '[]',
    problems_json      TEXT NOT NULL DEFAULT '[]',
    extracted_by       TEXT NOT NULL,              -- model id, or "demo"
    extraction_usd     TEXT NOT NULL DEFAULT '0',
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'posting', 'posted', 'post_failed', 'rejected', 'expired')),
    version            INTEGER NOT NULL DEFAULT 1, -- bumped on every change; approvals name the version they saw
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    decided_by         TEXT,
    decided_at         TEXT,
    reject_reason      TEXT,
    post_key           TEXT,                       -- idempotency key, fixed at the first approval
    post_state_json    TEXT NOT NULL DEFAULT '{}',
    posting_started_at TEXT,
    posted_ref         TEXT,
    posted_at          TEXT,
    last_error         TEXT
);
CREATE INDEX drafts_status_id ON drafts (status, id DESC);
CREATE INDEX drafts_open_expiry ON drafts (expires_at) WHERE status IN ('pending', 'post_failed');

CREATE TABLE invoices (
    id              INTEGER PRIMARY KEY,
    draft_id        INTEGER NOT NULL UNIQUE REFERENCES drafts (id), -- one invoice per draft, ever
    idempotency_key TEXT NOT NULL UNIQUE,
    customer_id     INTEGER NOT NULL REFERENCES customers (id),
    lines_json      TEXT NOT NULL,
    total           TEXT NOT NULL,
    issued_on       TEXT NOT NULL,
    posted_by       TEXT NOT NULL,  -- the approver's username: the record is theirs, not the agent's
    posted_by_name  TEXT NOT NULL,
    drafted_by      TEXT NOT NULL,  -- the agent that proposed it, kept for the record
    created_at      TEXT NOT NULL
);
CREATE TRIGGER invoices_no_update BEFORE UPDATE ON invoices
BEGIN SELECT RAISE(ABORT, 'invoices are immutable once posted'); END;
CREATE TRIGGER invoices_no_delete BEFORE DELETE ON invoices
BEGIN SELECT RAISE(ABORT, 'invoices are immutable once posted'); END;

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    draft_id    INTEGER,
    event       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_draft ON audit_log (draft_id, id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

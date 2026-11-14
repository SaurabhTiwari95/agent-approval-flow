-- Sign-in state lives in the database, not in the process, so any number of app instances can serve it.

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, -- sha256 of the cookie value; the raw token is never stored
    username   TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX sessions_expiry ON sessions (expires_at);

CREATE TABLE login_failures (
    id  INTEGER PRIMARY KEY,
    key TEXT NOT NULL, -- "ip:<address>" or "user:<username>"
    at  TEXT NOT NULL
);
CREATE INDEX login_failures_key_at ON login_failures (key, at);

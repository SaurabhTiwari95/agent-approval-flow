-- Claude spend per extraction. A row is reserved at the worst-case cost before the call and settled
-- with the real cost from the API's usage after it, so the daily cap holds even across processes.

CREATE TABLE extraction_costs (
    id                 INTEGER PRIMARY KEY,
    day                TEXT NOT NULL, -- UTC date
    at                 TEXT NOT NULL,
    source_name        TEXT NOT NULL,
    model              TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('reserved', 'settled')),
    usd_micros         INTEGER NOT NULL, -- millionths of a dollar: exact integer sums
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX extraction_costs_day ON extraction_costs (day);

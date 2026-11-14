# Changelog

## Unreleased

- Default extraction model is now `claude-haiku-5-5` (effort low, plain endpoint, no fallbacks);
  `EXTRACT_MODEL=claude-opus-5-5` restores the Opus path with refusal fallbacks.
- Pricing includes Haiku 5.5's higher rate for prompts over 100K tokens, for both the cap check and the
  recorded cost.

## 0.1.0 (2026-11-09)

- Order emails become PENDING draft invoices: Claude (claude-opus-5-5, effort low, structured JSON output,
  server-side refusal fallbacks, cached system prompt and catalogue) proposes customer, lines and date;
  the server matches them to its own customers and products and prices every line.
- Offline, rule-based extractor (`--extractor demo`) so the whole app runs without an API key.
- Approver web app (FastAPI, Jinja2, no JavaScript, strict CSP): sign in, list, side-by-side review, edit,
  approve, reject with a reason; approver and viewer roles.
- Approval rules: per-draft ceiling, per-approver limit, 48-hour expiry, version check, one post per draft
  under concurrency; posted under the approver's name.
- Posting targets: local invoices table, or governed-write-api (signed validate, total check, commit with
  an Idempotency-Key, safe retries).
- Append-only audit log (database triggers) for every step; immutable posted invoices.
- argon2id passwords, server-side sessions, CSRF tokens, login throttling, per-IP rate limit.
- Claude spend per extraction recorded; daily spend cap; `costs` report.
- CLI, Docker image, compose stack, CI (tests, lint, audit, secret scan, Docker smoke, docs), manual live
  smoke workflow.

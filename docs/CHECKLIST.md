# Release checklist

The same checklist every one of my projects meets before publishing. Ticked only when verified;
anything checked only by CI (not yet run) says so.

Last checked: 2026-11-09

## A. Installation and usage

- [x] What it is, with a diagram (Mermaid sequence)
- [x] Prerequisites with versions and links
- [x] Install steps for Windows (PowerShell) and macOS/Linux; Docker compose
- [x] Usage with real output: ingest, duplicate, drafts, missing-key message, the posted draft page (run locally with uvicorn and the offline extractor)
- [ ] Real output from Claude: **not run, since it calls the paid API and needs your key.** The README shows the reply's shape and says so; the manual live smoke workflow runs it when you choose
- [ ] Screenshots of the UI: not taken yet (the page text is shown instead)
- [x] One command for tests
- [x] Configuration table for every setting
- [x] Troubleshooting
- [x] Security model, design decisions, how it scales, performance, what's next
- [x] AI-assisted line
- [x] Relative links and anchors checked (`scripts/check_docs.py`: 0 problems); external links checked in CI by lychee
- [ ] Every command tested from a fresh clone: the Python path was run locally from a clean folder; `docker compose` only in CI (Docker isn't installed here)

## B. No private data

- [x] Made-up "Acme Traders" customers, products, people and emails (`example.com`), written for this project
- [x] No employer, product or people names; no real hosts, IPs, account ids or keys
- [x] No work code, names, counts or costs
- [x] Pre-publish check: 0 hits locally against the private block-list; also runs in CI
- [ ] gitleaks: CI only

## C. Secure by default

- [x] Secrets only from the environment; `.env` git-ignored; API key read from the shell only; `.env.example` holds placeholders
- [x] Input validated at the edge: Pydantic settings and agent output, form field limits, body size limit, quantity and line limits
- [x] Parameterised SQL only
- [x] Agent can't price, approve or post; exact matching; email framed as data (prompt-injection sample included and tested)
- [x] argon2id hashes, server-side sessions, `HttpOnly` + `SameSite=Strict` + `Secure`/`__Host-` cookies, CSRF on every POST, sign-in throttling, roles (tested)
- [x] Rate limit on every page (per IP, per process); strict CSP with no scripts; security headers (tested)
- [x] Errors never leak stack traces; passwords never logged (tested)
- [x] Append-only audit log and immutable invoices enforced by triggers (tested)
- [x] Dependencies pinned; anthropic held at 2.3.0; **pip-audit: no known vulnerabilities** (2026-11-09)
- [x] Dependabot (pip, Actions, Docker); CodeQL workflow; gitleaks in CI
- [x] Docker: multi-stage, slim, non-root uid 10001 (checked in CI), read-only filesystem and no capabilities in compose
- [x] `SECURITY.md`
- [ ] Least-privilege database role: not applicable to SQLite; the Postgres path is described in the README
- [ ] CodeQL results: first run on GitHub after the push
- [ ] Repo settings on GitHub after the push: secret scanning + push protection, private vulnerability reporting, branch protection

## D. Scalable

- [x] Sessions, throttling, spend and drafts in the database, not the process
- [ ] More than one app instance: not with SQLite (one writer). README explains the PostgreSQL path
- [x] All config from the environment (12-factor); the same image runs locally and in CI
- [x] Migrations (numbered SQL files, applied in order, recorded); indexes on every lookup column
- [x] Pagination on the drafts list; no unbounded queries (audit trail capped)
- [x] Extraction is a batch command, never inside a web request
- [ ] Posting runs inside the approve request (safe to retry, but not queued); listed in "What's next"
- [x] `/healthz` and `/readyz`
- [x] "How it scales" section with 10× and 100×

## E. Optimised

- [x] Profiled before optimising: write-lock waits found and fixed (p99 about 1.1-1.8 s to under 0.35 s)
- [x] No N+1: one query per list page
- [x] Catalogue cached in process (60 s, cleared on import)
- [x] Prompt caching: system prompt + catalogue as a byte-stable prefix with a breakpoint (tested identical across emails)
- [x] Cheapest model that does the job: Haiku 5.5 (decided 10 Nov 2026), effort `low` for short emails; `max_tokens` bounded; daily spend cap; duplicate emails never sent twice
- [ ] Async I/O: handlers are synchronous and run in FastAPI's thread pool (SQLite is synchronous); fine at this scale, revisit with PostgreSQL
- [x] Multi-stage Docker image
- [x] Performance numbers in the README (3 runs, real HTTP, `scripts/loadtest.py`)

## F. Repo basics

- [x] Tests: 150 passed, 98% coverage (gate 80% in CI); no network, no key, no cost
- [x] Lint: ruff check and format clean
- [x] Markdown and YAML checked locally (`scripts/check_docs.py`); full markdownlint in CI
- [x] `.gitignore` (including `data/`, `*.db`, `.env`), `LICENSE` (MIT), `CHANGELOG.md`, `docker-compose.yml`
- [x] Posting to governed-write-api checked against that project's real app (in-process, in-memory repository): posted once, second approval refused, a different price refused with `price_mismatch`

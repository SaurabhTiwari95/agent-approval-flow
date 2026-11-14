# Security policy

This is a personal demonstration project. Please report a security problem privately:
**Security** tab → **Report a vulnerability**. Please don't open a public issue.
I aim to reply within 7 days.

## What's in place

- The agent only proposes. It never prices anything, never approves anything and never posts anything.
  Every invoice is posted by a signed-in person, under their name, after the server re-checks the rules.
- Order emails are treated as data: the system prompt says so, the output is a fixed JSON schema with no
  place for a price, and the result is validated again on the server.
- Passwords: argon2id hashes in configuration, never stored or logged in plain text. Unknown usernames
  cost the same time as known ones. Sign-in is throttled per IP and per username.
- Sessions: a random token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` and `__Host-` prefixed
  unless `COOKIE_SECURE=false` for local HTTP). Only the token's SHA-256 is stored, with an expiry.
- CSRF token on every form; strict Content Security Policy with no scripts; frame, sniffing and referrer
  protections; `Cache-Control: no-store` on pages.
- Parameterised SQL only. The audit log and posted invoices refuse UPDATE and DELETE (database triggers).
- Requests to governed-write-api are HMAC-signed with a timestamp and nonce, and carry an Idempotency-Key.
- Errors show a generic page; details go to the server log only.
- Pinned dependencies, pip-audit, Dependabot, CodeQL, gitleaks; a non-root container that runs with a
  read-only filesystem and no Linux capabilities.

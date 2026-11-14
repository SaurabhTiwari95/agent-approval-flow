"""Command line.

python -m approval setup                     create tables and the sample catalogue
python -m approval ingest [PATH ...]         draft invoices from order emails (default: sample_orders/)
                   [--extractor demo|claude]  demo = offline, rule-based; claude = the AI agent
python -m approval drafts [--status pending]  list drafts
python -m approval expire                    mark drafts past their expiry (run from cron)
python -m approval cleanup                   delete expired sessions and old login failures
python -m approval costs [--days 7]          Claude spend per day and model
python -m approval import-products FILE.csv  load a price list (id,sku,name,unit_price)
python -m approval hash-password [--stdin]   make an argon2 hash for APPROVERS
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from approval import catalogue as cat
from approval.budget import SpendLedger
from approval.config import Settings
from approval.db import Database
from approval.drafts import DraftService
from approval.extractor import ClaudeExtractor, DemoExtractor

ROOT = Path(__file__).resolve().parent.parent
NO_KEY_HINT = (
    "EXTRACTOR=claude needs your own Anthropic API key in this shell (PowerShell: $env:ANTHROPIC_API_KEY = '...'; "
    "macOS/Linux: export ANTHROPIC_API_KEY=...), or run with --extractor demo to stay offline. "
    "Nothing was sent and nothing was charged."
)


def default_client_factory(settings: Settings):
    import anthropic

    return anthropic.Anthropic(timeout=settings.request_timeout_seconds, max_retries=settings.max_retries)


def _setup(db: Database) -> str:
    applied = db.migrate()
    customers, products = cat.seed(db)
    return f"migrations applied: {len(applied)}; sample customers added: {customers}; products added: {products}"


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in [Path(x) for x in paths] or [ROOT / "sample_orders"]:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in (".txt", ".eml"))
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(str(p))
    return out


def cmd_ingest(args, settings: Settings, db: Database, client_factory) -> int:
    kind = args.extractor or settings.extractor
    catalogue = cat.Catalogue(db)
    if kind == "claude":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print(NO_KEY_HINT, file=sys.stderr)
            return 2
        ledger = SpendLedger(db, settings.extract_day_cap_usd)
        extractor = ClaudeExtractor(settings, client_factory(settings), ledger, catalogue)
    else:
        extractor = DemoExtractor()
    try:
        files = _files(args.paths)
    except FileNotFoundError as exc:
        print(f"Not found: {exc}", file=sys.stderr)
        return 2
    # Ingestion doesn't post anything, so no poster is needed here.
    service = DraftService(settings, db, catalogue, poster=None)
    skipped = 0
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        o = service.ingest(f.name, text, extractor)
        skipped += o.status == "skipped"
        if o.status == "duplicate":
            print(f"{f.name:<36} duplicate draft {o.draft_id} already exists (not sent to the agent again)")
            continue
        draft = f"draft {o.draft_id}" if o.draft_id else "-"
        problems = ", ".join(o.problems) or "-"
        print(
            f"{f.name:<36} {o.status:<9} {draft:<9} {o.lines} lines  total {o.total:>9}  "
            f"${o.usd:.4f}  problems: {problems}"
        )
        if o.status in ("skipped", "too_large"):
            print(f"  {o.detail}")
    if kind == "claude":
        print(f"Claude spend today: ${extractor.ledger.committed():.4f} of ${settings.extract_day_cap_usd:.2f}")
    return 3 if skipped else 0


def cmd_drafts(args, settings: Settings, db: Database) -> int:
    service = DraftService(settings, db, cat.Catalogue(db), poster=None)
    rows, _ = service.list(None if args.status == "all" else args.status, 1)
    print(f"{'#':>4}  {'status':<11} {'customer':<28} {'lines':>5} {'total':>10}  source")
    for r in rows:
        print(
            f"{r['id']:>4}  {r['status']:<11} {(r['customer'] or 'not matched')[:28]:<28} {r['lines']:>5} "
            f"{r['total']:>10}  {r['source_name']}"
        )
    return 0


def cmd_costs(args, settings: Settings, db: Database) -> int:
    rows = SpendLedger(db, settings.extract_day_cap_usd).report(args.days)
    if not rows:
        print("No Claude spend recorded.")
        return 0
    for r in rows:
        print(
            f"{r['day']}  {r['model']:<20} calls {r['calls']:>4}  ${r['usd']:.4f}  "
            f"in {r['input_tokens']} out {r['output_tokens']} cache-read {r['cache_read_tokens']}"
        )
    return 0


def cmd_hash(args) -> int:
    from approval.auth import hash_password

    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != password:
            print("The passwords don't match.", file=sys.stderr)
            return 1
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m approval", description="agent-approval-flow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the tables")
    sub.add_parser("setup", help="migrate and add the sample catalogue")
    p = sub.add_parser("ingest", help="draft invoices from order emails")
    p.add_argument("paths", nargs="*", help="files or folders (default: sample_orders/)")
    p.add_argument("--extractor", choices=["claude", "demo"], help="overrides EXTRACTOR")
    p = sub.add_parser("drafts", help="list drafts")
    p.add_argument(
        "--status", default="all", choices=["pending", "posting", "posted", "post_failed", "rejected", "expired", "all"]
    )
    sub.add_parser("expire", help="mark drafts past their expiry")
    sub.add_parser("cleanup", help="delete expired sessions and old login failures")
    p = sub.add_parser("costs", help="Claude spend per day and model")
    p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("import-products", help="load a price list CSV (id,sku,name,unit_price)")
    p.add_argument("file")
    p = sub.add_parser("hash-password", help="print an argon2 hash for APPROVERS")
    p.add_argument("--stdin", action="store_true", help="read the password from standard input")
    return parser


def main(argv: list[str] | None = None, client_factory=default_client_factory) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hash-password":
        return cmd_hash(args)
    settings = Settings()
    db = Database(settings.database_path)
    try:
        if args.command == "migrate":
            print(f"migrations applied: {db.migrate() or 'none (up to date)'}")
            return 0
        if args.command == "setup":
            print(_setup(db))
            return 0
        db.migrate()
        if args.command == "ingest":
            return cmd_ingest(args, settings, db, client_factory)
        if args.command == "drafts":
            return cmd_drafts(args, settings, db)
        if args.command == "expire":
            n = DraftService(settings, db, cat.Catalogue(db), poster=None).sweep_expired()
            print(f"expired {n} draft(s)")
            return 0
        if args.command == "cleanup":
            from approval.auth import Auth

            print(f"deleted {Auth(settings, db).cleanup()} expired session/login rows")
            return 0
        if args.command == "costs":
            return cmd_costs(args, settings, db)
        if args.command == "import-products":
            try:
                n = cat.import_products_csv(db, Path(args.file))
            except (OSError, ValueError) as exc:
                print(f"Import refused, nothing changed: {exc}", file=sys.stderr)
                return 2
            print(f"imported {n} product(s)")
            return 0
    finally:
        db.close()
    return 2  # pragma: no cover - argparse rejects unknown commands

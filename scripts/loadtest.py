"""Load test over real HTTP: starts the app (uvicorn, one process) on a free local port with a fresh
SQLite file, creates drafts with the offline extractor, then has N approvers work in parallel:
list the drafts, open one, approve it (which posts it to the local invoices table).

    python scripts/loadtest.py --drafts 500 --concurrency 10

No Claude calls, no network beyond 127.0.0.1. Prints throughput and latency per step.
"""

from __future__ import annotations

import argparse
import re
import socket
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from approval import catalogue as cat  # noqa: E402
from approval.auth import hash_password  # noqa: E402
from approval.config import Approver, Settings  # noqa: E402
from approval.db import Database  # noqa: E402
from approval.drafts import DraftService  # noqa: E402
from approval.extractor import DemoExtractor  # noqa: E402
from approval.posting import LocalPoster  # noqa: E402
from approval.web import create_app  # noqa: E402

CSRF = re.compile(r'name="csrf" value="([^"]+)"')
VERSION = re.compile(r'name="version" value="(\d+)"')
PASSWORD = "load-test-password-only"  # throwaway, for this temporary database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values: list[float], p: float) -> float:
    return statistics.quantiles(values, n=100)[int(p) - 1] if len(values) > 1 else values[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drafts", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp())
    users = [f"approver{i}" for i in range(args.concurrency)]
    pw_hash = hash_password(PASSWORD)
    settings = Settings(
        _env_file=None,
        database_path=str(tmp / "load.db"),
        cookie_secure=False,
        rate_limit_per_minute=100_000,
        log_level="WARNING",
        approvers={u: Approver(name=u, hash=pw_hash, role="approver") for u in users},
        max_draft_total=Decimal("10000000"),
    )
    db = Database(settings.database_path)
    db.migrate()
    cat.seed(db)
    service = DraftService(settings, db, cat.Catalogue(db), LocalPoster(db))
    sample = (ROOT / "sample_orders" / "order-001-kestrel.txt").read_text(encoding="utf-8")
    t0 = time.perf_counter()
    ids = [service.ingest(f"load-{i}.txt", f"{sample}\nref {i}", DemoExtractor()).draft_id for i in range(args.drafts)]
    print(f"ingested {len(ids)} drafts in {time.perf_counter() - t0:.2f} s (offline extractor)")

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    timings: dict[str, list[float]] = {"list": [], "open": [], "approve": []}
    lock = threading.Lock()
    queue = list(ids)

    def worker(user: str) -> int:
        done = 0
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
            page = c.get("/login").text
            c.post("/login", data={"username": user, "password": PASSWORD, "csrf": CSRF.search(page).group(1)})
            while True:
                with lock:
                    if not queue:
                        return done
                    draft_id = queue.pop()
                for step, call in (
                    ("list", lambda: c.get("/drafts")),
                    ("open", lambda d=draft_id: c.get(f"/drafts/{d}")),
                ):
                    s = time.perf_counter()
                    r = call()
                    timings[step].append(time.perf_counter() - s)
                    assert r.status_code == 200, r.status_code
                html = r.text
                s = time.perf_counter()
                r = c.post(
                    f"/drafts/{draft_id}/approve",
                    data={"csrf": CSRF.search(html).group(1), "version": VERSION.search(html).group(1)},
                )
                timings["approve"].append(time.perf_counter() - s)
                assert r.status_code == 303 and r.headers["location"].endswith("?m=posted"), r.status_code
                done += 1

    start = time.perf_counter()
    with ThreadPoolExecutor(args.concurrency) as pool:
        approved = sum(pool.map(worker, users))
    elapsed = time.perf_counter() - start
    server.should_exit = True

    invoices = db.one("SELECT count(*) AS n FROM invoices")["n"]
    requests = sum(len(v) for v in timings.values())
    print(f"approved {approved} drafts ({invoices} invoices) in {elapsed:.2f} s with {args.concurrency} approvers")
    print(f"  {approved / elapsed:.0f} approvals/s, {requests / elapsed:.0f} requests/s")
    for step, values in timings.items():
        ms = [v * 1000 for v in values]
        print(f"  {step:<8} p50 {pct(ms, 50):6.1f} ms   p95 {pct(ms, 95):6.1f} ms   p99 {pct(ms, 99):6.1f} ms")
    db.close()
    return 0 if invoices == approved == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end smoke check against a running app: sign in, open the first draft that can be approved,
approve it, and confirm it was posted. Used by CI against the Docker image; handy after a deploy.

    SMOKE_PASSWORD=... python scripts/smoke.py --url http://127.0.0.1:8010 --username dana

The password is read from the environment, never from the command line (it would land in shell history).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import httpx

CSRF = re.compile(r'name="csrf" value="([^"]+)"')
VERSION = re.compile(r'name="version" value="(\d+)"')
DRAFT_LINK = re.compile(r'href="/drafts/(\d+)"')


def run(url: str, username: str, password: str, client: httpx.Client | None = None) -> str:
    c = client or httpx.Client(base_url=url, timeout=15)
    page = c.get("/login")
    r = c.post("/login", data={"username": username, "password": password, "csrf": CSRF.search(page.text).group(1)})
    if r.status_code != 303:
        raise RuntimeError(f"sign-in failed: HTTP {r.status_code}")
    listing = c.get("/drafts?status=pending").text
    for draft_id in dict.fromkeys(DRAFT_LINK.findall(listing)):
        html = c.get(f"/drafts/{draft_id}").text
        if "Approve and post as" not in html or "Before it can be approved" in html:
            continue
        r = c.post(
            f"/drafts/{draft_id}/approve",
            data={"csrf": CSRF.search(html).group(1), "version": VERSION.search(html).group(1)},
        )
        done = c.get(r.headers.get("location", f"/drafts/{draft_id}")).text
        ref = re.search(r"Posted as <strong>([^<]+)</strong>", done)
        if r.status_code != 303 or not ref:
            raise RuntimeError(f"approving draft {draft_id} did not post (HTTP {r.status_code})")
        return f"draft {draft_id} approved and posted as {ref.group(1)}"
    raise RuntimeError("no pending draft could be approved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    parser.add_argument("--username", default="dana")
    args = parser.parse_args(argv)
    password = os.environ.get("SMOKE_PASSWORD", "")
    if not password:
        print("Set SMOKE_PASSWORD", file=sys.stderr)
        return 2
    try:
        print(run(args.url, args.username, password))
    except (RuntimeError, httpx.HTTPError, AttributeError) as exc:
        print(f"smoke check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

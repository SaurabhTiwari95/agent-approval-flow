"""Create .env from .env.example with fresh random secrets and three demo sign-ins.

    python scripts/make_env.py            # refuses to overwrite an existing .env
    python scripts/make_env.py --force

The demo passwords are printed once, here, and never stored: only their argon2 hashes go into .env.
The write-API secret is random too; give governed-write-api the same one in its CLIENT_KEYS if you
post there. .env is git-ignored.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from approval.auth import hash_password  # noqa: E402

DEMO_PEOPLE = {  # made-up people
    "dana": {"name": "Dana Whitfield", "role": "approver", "limit": "20000.00"},
    "lee": {"name": "Lee Okafor", "role": "approver", "limit": None},
    "sam": {"name": "Sam Ortega", "role": "viewer", "limit": None},
}


def build_env(example: str) -> tuple[str, dict[str, str]]:
    passwords = {user: secrets.token_urlsafe(12) for user in DEMO_PEOPLE}
    approvers = {}
    for user, info in DEMO_PEOPLE.items():
        entry = {"name": info["name"], "hash": hash_password(passwords[user]), "role": info["role"]}
        if info["limit"]:
            entry["limit"] = info["limit"]
        approvers[user] = entry
    values = {
        "APPROVERS": "'" + json.dumps(approvers, separators=(",", ":")) + "'",
        "WRITEAPI_SECRET": secrets.token_urlsafe(32),
    }
    lines = []
    for line in example.splitlines():
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in values:
            line = f"{key}={values[key]}"
        lines.append(line)
    return "\n".join(lines) + "\n", passwords


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target = root / ".env"
    if target.exists() and "--force" not in argv:
        print(".env already exists; run with --force to replace it", file=sys.stderr)
        return 1
    text, passwords = build_env((root / ".env.example").read_text(encoding="utf-8"))
    target.write_text(text, encoding="utf-8")
    print("wrote .env (git-ignored; never commit it). Demo sign-ins, shown only now:")
    for user, info in DEMO_PEOPLE.items():
        limit = f", limit {info['limit']}" if info["limit"] else ""
        print(f"  {user:<5} {passwords[user]}   ({info['role']}{limit})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from approval import cli
from approval.config import Settings
from tests.conftest import SAMPLES
from tests.fakes import FakeClaude, ok_response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import make_env  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here: settings come from the environment only
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return tmp_path


def run(capsys, *argv, factory=None):
    code = cli.main(list(argv), client_factory=factory or (lambda s: pytest.fail("no client expected")))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_setup_ingest_list_expire(env, capsys):
    code, out, _ = run(capsys, "setup")
    assert code == 0 and "migrations applied: 3; sample customers added: 6; products added: 12" in out
    code, out, _ = run(capsys, "ingest", "--extractor", "demo")
    assert code == 0 and out.count(" created ") == 5 and "problems: unknown_customer" in out
    code, out, _ = run(capsys, "ingest", "--extractor", "demo", str(SAMPLES / "order-001-kestrel.txt"))
    assert "duplicate draft 1 already exists" in out
    code, out, _ = run(capsys, "drafts", "--status", "pending")
    assert "Kestrel Kitchenware" in out and "78000.00" in out
    code, out, _ = run(capsys, "expire")
    assert out.strip() == "expired 0 draft(s)"
    code, out, _ = run(capsys, "cleanup")
    assert code == 0
    code, out, _ = run(capsys, "migrate")
    assert "none (up to date)" in out


def test_ingest_with_claude_needs_a_key_and_sends_nothing(env, capsys):
    run(capsys, "setup")
    code, _, err = run(capsys, "ingest", "--extractor", "claude")
    assert code == 2 and "Nothing was sent and nothing was charged" in err


def test_ingest_with_fake_claude(env, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    fake = FakeClaude([ok_response(), ok_response()])
    run(capsys, "setup")
    code, out, _ = run(
        capsys, "ingest", "--extractor", "claude", str(SAMPLES / "order-001-kestrel.txt"), factory=lambda s: fake
    )
    assert code == 0 and "created" in out and "Claude spend today: $0.0003" in out
    code, out, _ = run(capsys, "costs")
    assert "claude-haiku-5-5" in out and "calls    1" in out


def test_ingest_stops_at_the_day_cap(env, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EXTRACT_DAY_CAP_USD", "0.001")
    run(capsys, "setup")
    code, out, _ = run(capsys, "ingest", "--extractor", "claude", factory=lambda s: FakeClaude([]))
    assert code == 3 and out.count("skipped") == 5 and "daily extraction cap" in out


def test_ingest_missing_path(env, capsys):
    code, _, err = run(capsys, "ingest", "--extractor", "demo", "nope.txt")
    assert code == 2 and "Not found" in err


def test_costs_empty(env, capsys):
    assert run(capsys, "costs")[1].strip() == "No Claude spend recorded."


def test_import_products(env, capsys, tmp_path):
    good = tmp_path / "p.csv"
    good.write_text("id,sku,name,unit_price\n1,SKU-00001,Bowl,13.00\n50,SKU-00050,New thing,1.5\n", encoding="utf-8")
    run(capsys, "setup")
    code, out, _ = run(capsys, "import-products", str(good))
    assert code == 0 and "imported 2" in out
    bad = tmp_path / "bad.csv"
    bad.write_text("id,sku,name,unit_price\nx,SKU-1,Bowl,abc\n", encoding="utf-8")
    code, _, err = run(capsys, "import-products", str(bad))
    assert code == 2 and "line 2" in err
    neg = tmp_path / "neg.csv"
    neg.write_text("id,sku,name,unit_price\n1,SKU-1,Bowl,-1\n", encoding="utf-8")
    assert run(capsys, "import-products", str(neg))[0] == 2
    assert run(capsys, "import-products", str(tmp_path / "missing.csv"))[0] == 2


def test_hash_password_from_stdin(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("a long enough password\n"))
    code, out, _ = run(capsys, "hash-password", "--stdin")
    assert code == 0 and out.startswith("$argon2id$")
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    assert run(capsys, "hash-password", "--stdin")[0] == 1


def test_hash_password_prompt(capsys, monkeypatch):
    answers = iter(["first password 1", "second password 2"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    code, _, err = run(capsys, "hash-password")
    assert code == 1 and "don't match" in err


def test_make_env_writes_hashes_not_passwords(tmp_path, capsys, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env.example").write_text((root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    assert make_env.main([], root=tmp_path) == 0
    printed = capsys.readouterr().out
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    passwords = [line.split()[1] for line in printed.splitlines()[1:]]
    assert len(passwords) == 3 and not any(p in text for p in passwords)
    assert make_env.main([], root=tmp_path) == 1  # never overwrites by accident
    # the generated file loads (single quotes keep the $ in argon2 hashes intact)
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert set(s.approvers) == {"dana", "lee", "sam"} and s.approvers["sam"].role == "viewer"
    assert s.approvers["dana"].hash.startswith("$argon2id$") and len(s.writeapi_secret) >= 32
    from approval.auth import verify_password

    assert verify_password(s.approvers["lee"].hash, passwords[1])


def test_settings_reject_bad_approvers():
    with pytest.raises(ValueError, match="argon2"):
        Settings(_env_file=None, approvers={"dana": {"name": "D", "hash": "plain-text-password-here"}})
    with pytest.raises(ValueError, match="username"):
        Settings(_env_file=None, approvers=json.loads('{"Bad Name": {"name": "D", "hash": "$argon2id$v=19$m=1$x$y"}}'))

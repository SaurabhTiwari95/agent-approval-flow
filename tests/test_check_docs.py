import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_docs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_this_repo_is_clean():
    assert check_docs.run(ROOT) == []


def test_catches_the_usual_mistakes(tmp_path):
    (tmp_path / "other.md").write_text("# Other\n\n## Real part\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "# Title\ntext\n```\ncode\n```\n\n\nA list:\n- one\n\n[ok](other.md#real-part) [bad](missing.md) "
        "[gone](other.md#nope) [web](https://example.com)  \n",
        encoding="utf-8",
    )
    (tmp_path / "b.yml").write_text("key: [unclosed\n", encoding="utf-8")
    found = "\n".join(check_docs.run(tmp_path))
    for expected in (
        "a.md:1: blank line needed after a heading",
        "a.md:3: fenced block without a language",
        "a.md:3: blank line needed before a fenced block",
        "a.md:7: two blank lines in a row",
        "a.md:9: blank line needed before a list",
        "a.md:11: trailing spaces",
        "broken link missing.md",
        "missing anchor other.md#nope",
        "b.yml: YAML does not parse",
    ):
        assert expected in found
    assert "real-part" not in found and "example.com" not in found


def test_slugs_match_github():
    assert check_docs.slug("How it `scales` (10×)") == "how-it-scales-10"
    assert check_docs.anchors("# A\n\n# A\n```bash\n# not a heading\n```\n") == {"a", "a-1"}


def test_main_exit_code(tmp_path, capsys):
    (tmp_path / "ok.md").write_text("# Fine\n", encoding="utf-8")
    assert check_docs.main([str(tmp_path)]) == 0
    (tmp_path / "bad.md").write_text("# Bad\nno gap\n", encoding="utf-8")
    assert check_docs.main([str(tmp_path)]) == 1

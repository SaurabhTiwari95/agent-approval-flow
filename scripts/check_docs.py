"""Local docs check, so problems show up before CI's markdownlint and lychee run.

Markdown (the rules most often broken): every fenced block names a language; headings and lists have
a blank line around them; no trailing spaces, no double blank lines, one final newline; relative links
point at files that exist, and #anchors at headings that exist (GitHub's slug rules).
YAML: every .yml/.yaml file parses.

    python scripts/check_docs.py [path]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "htmlcov"}
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


def slug(heading: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        m = HEADING.match(line)
        if m and not in_fence:
            s = slug(m.group(2))
            n = seen.get(s, 0)
            out.add(s if n == 0 else f"{s}-{n}")
            seen[s] = n + 1
    return out


def check_markdown(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    lines = text.split("\n")
    problems: list[str] = []
    if not text.endswith("\n") or text.endswith("\n\n"):
        problems.append(f"{rel}: must end with exactly one newline")
    in_fence = False
    for i, line in enumerate(lines, start=1):
        prev = lines[i - 2] if i >= 2 else ""
        nxt = lines[i] if i < len(lines) else ""
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_fence and stripped == "```":
                problems.append(f"{rel}:{i}: fenced block without a language")
            if not in_fence and prev.strip() and not LIST_ITEM.match(prev):
                problems.append(f"{rel}:{i}: blank line needed before a fenced block")
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line != line.rstrip():
            problems.append(f"{rel}:{i}: trailing spaces")
        if not line.strip() and not prev.strip() and i > 1:
            problems.append(f"{rel}:{i}: two blank lines in a row")
        if HEADING.match(line):
            if i > 1 and prev.strip():
                problems.append(f"{rel}:{i}: blank line needed before a heading")
            if nxt.strip():
                problems.append(f"{rel}:{i}: blank line needed after a heading")
        if LIST_ITEM.match(line) and prev.strip() and not LIST_ITEM.match(prev) and not prev.startswith((" ", "\t")):
            problems.append(f"{rel}:{i}: blank line needed before a list")
    for target in LINK.findall(re.sub(r"```.*?```", "", text, flags=re.S)):
        if re.match(r"^[a-z]+:", target):
            continue  # external (checked by lychee in CI) or mailto
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            problems.append(f"{rel}: broken link {target}")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest.read_text(encoding="utf-8")):
            problems.append(f"{rel}: missing anchor {target}")
    return problems


def check_yaml(path: Path, root: Path) -> list[str]:
    import yaml

    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{path.relative_to(root).as_posix()}: YAML does not parse ({type(exc).__name__})"]
    return []


def run(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts) or not path.is_file():
            continue
        if path.suffix == ".md":
            problems += check_markdown(path, root)
        elif path.suffix in (".yml", ".yaml"):
            problems += check_yaml(path, root)
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0] if argv else ".").resolve()
    problems = run(root)
    for p in problems:
        print(p)
    print(f"check-docs: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

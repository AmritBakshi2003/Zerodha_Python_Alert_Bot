"""F23: line endings must not depend on the contributor's Git settings.

The demo writes LF, and the committed golden file is LF in the index. Without
``.gitattributes`` a Windows checkout with ``core.autocrlf=true`` presents it as
CRLF, so a raw ``diff`` fails even though the script that produced it is correct.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _rules() -> list[str]:
    path = ROOT / ".gitattributes"
    assert path.is_file(), "the repo has no .gitattributes (F23)"
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_gitattributes_pins_lf_for_text_files() -> None:
    rules = _rules()
    assert "* text=auto eol=lf" in rules, rules
    assert "*.png binary" in rules, rules


def test_golden_demo_output_is_checked_out_with_lf() -> None:
    result = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", "demo_output.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "eol: lf" in result.stdout, result.stdout


def test_no_tracked_source_file_is_stored_with_crlf() -> None:
    """The index is the source of truth: CRLF there breaks Linux diffs and tools."""
    result = subprocess.run(
        ["git", "ls-files", "--eol", "--", "*.py", "*.md", "*.yml", "*.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    offending = [line for line in result.stdout.splitlines() if "i/crlf" in line]
    assert not offending, "tracked text files are stored with CRLF:\n" + "\n".join(offending)

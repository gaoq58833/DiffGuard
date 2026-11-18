from pathlib import Path

import pytest

from diffguard.tools.diff import parse_diff

PATCH = (Path(__file__).parent / "fixtures" / "sample.patch").read_text(encoding="utf-8")


@pytest.fixture()
def files():
    return parse_diff(PATCH)


def test_parses_both_files(files):
    assert [f.path for f in files] == ["app/db.py", "app/safe.py"]


def test_hunk_line_numbers_are_real(files):
    db = files[0]
    added = {ln.new_lineno for ln in db.added_lines}
    assert added == {2, 3, 4, 5, 6, 7, 8, 9}


def test_removed_lines_carry_old_lineno_only(files):
    db = files[0]
    removed = [ln for h in db.hunks for ln in h.lines if ln.kind == "-"]
    assert [ln.old_lineno for ln in removed] == [2, 3]
    assert all(ln.new_lineno is None for ln in removed)


def test_language_detection(files):
    assert files[0].language == "python"
    assert files[1].language == "python"


def test_handles_empty_and_binary_safely():
    assert parse_diff("") == []
    binary = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    result = parse_diff(binary)
    assert len(result) == 1 and result[0].is_binary

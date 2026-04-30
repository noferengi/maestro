import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_read_file")

import app.agent.tools as tools


def _reset_prepped():
    """Clear the ContextVar so each test starts with no served-range history."""
    tools._prepped_files.set(None)


def _make_test_file(path: str, n_lines: int = 30) -> None:
    with open(path, "w") as fh:
        for i in range(1, n_lines + 1):
            fh.write(f"Line {i}\n")


def test_served_range_recorded_after_first_read():
    """read_file records the served range in the ContextVar dict."""
    _reset_prepped()
    test_file = os.path.abspath("_test_reread_tmp.txt")
    _make_test_file(test_file, n_lines=30)
    try:
        norm = os.path.normpath(os.path.realpath(test_file))

        # Prep the file (structural summary, no lines served yet)
        tools.read_file(test_file)
        assert norm in tools._get_prepped_files(), "File not marked as prepped"
        assert tools._get_prepped_files()[norm] == [], "No ranges should be served yet"

        # Serve lines 1-10
        result = tools.read_file(test_file, start=1, end=10)
        assert "Line 1" in result and "Line 10" in result, f"Expected content, got: {result[:200]}"
        assert "NOTE:" not in result, "Should not have NOTE on first read"

        # ContextVar dict must now record (1, 10)
        served = tools._get_prepped_files().get(norm, [])
        assert (1, 10) in served, f"Range (1,10) not recorded. Served: {served}"
        logger.info("PASS: served range recorded: %s", served)
    finally:
        os.remove(test_file)


def test_reread_same_range_returns_note():
    """Re-reading an already-served range prefixes the result with a NOTE."""
    _reset_prepped()
    test_file = os.path.abspath("_test_reread_tmp.txt")
    _make_test_file(test_file, n_lines=30)
    try:
        tools.read_file(test_file)
        tools.read_file(test_file, start=1, end=10)  # first serve

        result = tools.read_file(test_file, start=1, end=10)  # re-read
        assert "NOTE:" in result and "already in context" in result, (
            f"Expected NOTE for re-read. Got: {result[:200]}"
        )
        assert "Line 1" in result, "NOTE re-read should still include content"
        logger.info("PASS: re-read returned NOTE prefix")
    finally:
        os.remove(test_file)


def test_partial_overlap_no_note():
    """Reading a range that extends beyond what was served returns content without NOTE."""
    _reset_prepped()
    test_file = os.path.abspath("_test_reread_tmp.txt")
    _make_test_file(test_file, n_lines=30)
    try:
        norm = os.path.normpath(os.path.realpath(test_file))
        tools.read_file(test_file)
        tools.read_file(test_file, start=1, end=10)  # serve (1, 10)

        # (5, 15) extends past 10 — not fully covered → no NOTE
        result = tools.read_file(test_file, start=5, end=15)
        assert "NOTE:" not in result, (
            f"Should NOT have NOTE for partially-new range (5-15). Got: {result[:200]}"
        )
        assert "Line 5" in result and "Line 15" in result, "Content missing"

        # After serving (5-15), merged range should be (1, 15)
        served = tools._get_prepped_files().get(norm, [])
        assert (1, 15) in served, f"Expected merged (1,15). Served: {served}"
        logger.info("PASS: partial overlap no NOTE, merged range: %s", served)
    finally:
        os.remove(test_file)


if __name__ == "__main__":
    test_served_range_recorded_after_first_read()
    test_reread_same_range_returns_note()
    test_partial_overlap_no_note()
    logger.info("All tests passed.")

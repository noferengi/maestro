"""
Tests for new tool helpers, slicing behaviour, truncation footer, and registry invariants.

Covers:
1. read_last_output slicing (head/tail/grep/offset+limit)
2. Structured truncation footer
3. New helper smoke tests (read_file_metadata, read_diff_stat, find_symbol)
4. Allowlist sanity — every name in every *_TOOLS list is in TOOL_REGISTRY
5. Side-effect tag coverage — every schema description starts with [READ/RUN/WRITE]
6. No-shell assertion — run_shell, run_shell_indev, run_shell_build, run_shell_deps absent
7. No-read-lines assertion — read_file_lines absent
8. Stash tools roundtrip (write_git_stash → read_git_stash_list → write_git_stash_pop)
9. find_in_files head/tail/grep params
10. run_test_pytest head/tail/grep params (patched shell)
"""

import os
import sys
import subprocess
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.tools import (
    TOOL_REGISTRY,
    TOOL_SCHEMAS,
    _output_buffer,
    _output_buffer_lock,
    _cap_tool_result,
    read_last_output,
    find_in_files,
    _task_git_cwd,
    set_task_git_cwd,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_buffer(text: str) -> None:
    with _output_buffer_lock:
        _output_buffer["_sync"] = text


# ---------------------------------------------------------------------------
# 1. read_last_output slicing
# ---------------------------------------------------------------------------

class TestReadLastOutput:
    def test_head(self):
        _seed_buffer("line1\nline2\nline3\nline4\nline5")
        result = read_last_output(head=3)
        lines = result.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]

    def test_tail(self):
        _seed_buffer("line1\nline2\nline3\nline4\nline5")
        result = read_last_output(tail=2)
        lines = result.strip().splitlines()
        assert lines == ["line4", "line5"]

    def test_grep(self):
        _seed_buffer("apple\nbanana\napricot\ncherry")
        result = read_last_output(grep="ap")
        assert "apple" in result
        assert "apricot" in result
        assert "banana" not in result

    def test_offset_and_limit(self):
        content = "\n".join(f"L{i}" for i in range(10))
        _seed_buffer(content)
        result = read_last_output(offset=3, limit=4)
        lines = result.strip().splitlines()
        assert lines == ["L3", "L4", "L5", "L6"]

    def test_empty_buffer(self):
        with _output_buffer_lock:
            _output_buffer.pop("_sync", None)
        result = read_last_output()
        assert "no previous" in result.lower() or result == "(no previous tool output in buffer)"

    def test_grep_combined_with_head(self):
        _seed_buffer("error: foo\nok: bar\nerror: baz\nok: qux\nerror: zap")
        result = read_last_output(grep="error", head=2)
        lines = result.strip().splitlines()
        assert len(lines) == 2
        assert all("error" in ln for ln in lines)


# ---------------------------------------------------------------------------
# 2. Structured truncation footer
# ---------------------------------------------------------------------------

class TestTruncationFooter:
    def test_footer_present_when_truncated(self):
        # _cap_tool_result truncates at 200 KiB; synthesise a 210 KiB string
        big = "x" * (210 * 1024)
        result = _cap_tool_result("dummy_tool", big)
        assert "[TRUNCATED]" in result
        assert "total_chars=" in result
        assert "shown_lines=" in result
        assert "next_offset_lines=" in result
        assert "hint=" in result

    def test_no_footer_when_small(self):
        small = "hello world\n" * 10
        result = _cap_tool_result("dummy_tool", small)
        assert "[TRUNCATED]" not in result
        assert result.strip() == small.strip()

    def test_footer_values_are_consistent(self):
        big = "line\n" * 60_000  # ~300 KB
        result = _cap_tool_result("dummy_tool", big)
        assert "[TRUNCATED]" in result
        # shown_lines should be less than total_lines
        import re
        total_m = re.search(r"total_lines=(\d+)", result)
        shown_m = re.search(r"shown_lines=(\d+)", result)
        assert total_m and shown_m
        assert int(shown_m.group(1)) < int(total_m.group(1))


# ---------------------------------------------------------------------------
# 3. New helper smoke tests
# ---------------------------------------------------------------------------

class TestReadFileMetadata:
    def test_basic(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world\n" * 5)
        token = _task_git_cwd.set(str(tmp_path))
        try:
            from app.agent.tools import read_file_metadata
            result = read_file_metadata(str(f))
            assert "size=" in result or "bytes" in result.lower() or "line" in result.lower()
        finally:
            _task_git_cwd.reset(token)

    def test_nonexistent(self, tmp_path):
        from app.agent.tools import read_file_metadata
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = read_file_metadata(str(tmp_path / "nope.txt"))
            assert "ERROR" in result or "not found" in result.lower() or "No such" in result
        finally:
            _task_git_cwd.reset(token)


class TestReadDiffStat:
    def test_no_cwd_returns_error(self):
        from app.agent.tools import read_diff_stat
        token = _task_git_cwd.set(None)
        try:
            result = read_diff_stat()
            assert "ERROR" in result
        finally:
            _task_git_cwd.reset(token)

    def test_with_git_repo(self, tmp_path):
        from app.agent.tools import read_diff_stat
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
        f = tmp_path / "a.py"
        f.write_text("x=1\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = read_diff_stat(since="HEAD")
            # Empty diff is valid; just confirm no crash
            assert isinstance(result, str)
        finally:
            _task_git_cwd.reset(token)


class TestFindSymbol:
    def test_finds_function(self, tmp_path):
        from app.agent.tools import find_symbol
        src = tmp_path / "mymodule.py"
        src.write_text("def frobnicate(x):\n    return x + 1\n\nclass MyWidget:\n    pass\n")
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = find_symbol("frobnicate")
            assert "frobnicate" in result
        finally:
            _task_git_cwd.reset(token)

    def test_finds_class(self, tmp_path):
        from app.agent.tools import find_symbol
        src = tmp_path / "mymodule.py"
        src.write_text("def foo(): pass\nclass Splendid:\n    pass\n")
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = find_symbol("Splendid", kind="class")
            assert "Splendid" in result
        finally:
            _task_git_cwd.reset(token)


# ---------------------------------------------------------------------------
# 4. Allowlist sanity
# ---------------------------------------------------------------------------

class TestAllowlistSanity:
    def test_all_tool_names_in_registry(self):
        from app.agent.config import (
            RESEARCH_AGENT_TOOLS,
            INDEV_AGENT_TOOLS,
            SECURITY_REVIEWER_TOOLS,
            FULL_REVIEW_CODE_QUALITY_TOOLS,
            FULL_REVIEW_FUNCTIONAL_TOOLS,
            SUBDIVISION_AGENT_TOOLS,
            SUBDIVISION_PLANNING_TOOLS,
            CONCEPTUAL_REVIEW_REVIEWER_TOOLS,
            OPTIMIZATION_REVIEWER_TOOLS,
            DREAMER_SURVEY_TOOLS,
        )
        all_lists = {
            "RESEARCH_AGENT_TOOLS": RESEARCH_AGENT_TOOLS,
            "INDEV_AGENT_TOOLS": INDEV_AGENT_TOOLS,
            "SECURITY_REVIEWER_TOOLS": SECURITY_REVIEWER_TOOLS,
            "FULL_REVIEW_CODE_QUALITY_TOOLS": FULL_REVIEW_CODE_QUALITY_TOOLS,
            "FULL_REVIEW_FUNCTIONAL_TOOLS": FULL_REVIEW_FUNCTIONAL_TOOLS,
            "SUBDIVISION_AGENT_TOOLS": SUBDIVISION_AGENT_TOOLS,
            "SUBDIVISION_PLANNING_TOOLS": SUBDIVISION_PLANNING_TOOLS,
            "CONCEPTUAL_REVIEW_REVIEWER_TOOLS": CONCEPTUAL_REVIEW_REVIEWER_TOOLS,
            "OPTIMIZATION_REVIEWER_TOOLS": OPTIMIZATION_REVIEWER_TOOLS,
            "DREAMER_SURVEY_TOOLS": DREAMER_SURVEY_TOOLS,
        }
        missing: dict[str, list[str]] = {}
        for list_name, tools in all_lists.items():
            absent = [t for t in tools if t not in TOOL_REGISTRY]
            if absent:
                missing[list_name] = absent
        assert not missing, f"Tool names in config not found in TOOL_REGISTRY: {missing}"


# ---------------------------------------------------------------------------
# 5. Side-effect tag coverage
# ---------------------------------------------------------------------------

VALID_TAGS = ("[READ]", "[RUN", "[WRITE")


class TestSideEffectTags:
    def test_all_schemas_tagged(self):
        untagged = []
        for schema in TOOL_SCHEMAS:
            fn = schema["function"]
            desc = fn.get("description", "")
            if isinstance(desc, tuple):
                desc = "".join(desc)
            if not any(desc.startswith(tag) for tag in VALID_TAGS):
                untagged.append(fn["name"])
        assert not untagged, f"Schema descriptions missing [READ/RUN/WRITE] tag: {untagged}"

    def test_write_tools_have_write_tag(self):
        for schema in TOOL_SCHEMAS:
            fn = schema["function"]
            name = fn["name"]
            desc = fn.get("description", "")
            if isinstance(desc, tuple):
                desc = "".join(desc)
            if name.startswith("write_"):
                assert desc.startswith("[WRITE"), f"{name}: write_* tool must start with [WRITE …], got: {desc[:40]!r}"

    def test_read_tools_have_read_tag(self):
        for schema in TOOL_SCHEMAS:
            fn = schema["function"]
            name = fn["name"]
            desc = fn.get("description", "")
            if isinstance(desc, tuple):
                desc = "".join(desc)
            if name.startswith("read_"):
                assert desc.startswith("[READ]"), f"{name}: read_* tool must start with [READ], got: {desc[:40]!r}"


# ---------------------------------------------------------------------------
# 6. No-shell assertion
# ---------------------------------------------------------------------------

class TestNoShell:
    @pytest.mark.parametrize("name", [
        "run_shell",
        "run_shell_indev",
        "run_shell_build",
        "run_shell_deps",
    ])
    def test_not_in_registry(self, name):
        assert name not in TOOL_REGISTRY, f"{name} must not be in TOOL_REGISTRY"

    @pytest.mark.parametrize("name", [
        "run_shell",
        "run_shell_indev",
        "run_shell_build",
        "run_shell_deps",
    ])
    def test_not_in_any_allowlist(self, name):
        from app.agent.config import (
            RESEARCH_AGENT_TOOLS, INDEV_AGENT_TOOLS, SECURITY_REVIEWER_TOOLS,
            FULL_REVIEW_CODE_QUALITY_TOOLS, FULL_REVIEW_FUNCTIONAL_TOOLS,
            SUBDIVISION_AGENT_TOOLS, SUBDIVISION_PLANNING_TOOLS,
            CONCEPTUAL_REVIEW_REVIEWER_TOOLS, OPTIMIZATION_REVIEWER_TOOLS,
        )
        all_tools = (
            RESEARCH_AGENT_TOOLS + INDEV_AGENT_TOOLS + SECURITY_REVIEWER_TOOLS +
            FULL_REVIEW_CODE_QUALITY_TOOLS + FULL_REVIEW_FUNCTIONAL_TOOLS +
            SUBDIVISION_AGENT_TOOLS + SUBDIVISION_PLANNING_TOOLS +
            CONCEPTUAL_REVIEW_REVIEWER_TOOLS + OPTIMIZATION_REVIEWER_TOOLS
        )
        assert name not in all_tools, f"{name} must not appear in any agent tool allowlist"


# ---------------------------------------------------------------------------
# 7. No-read-lines assertion
# ---------------------------------------------------------------------------

class TestNoReadFileLines:
    def test_not_in_registry(self):
        assert "read_file_lines" not in TOOL_REGISTRY

    def test_not_in_tools_source(self):
        tools_path = os.path.join(os.path.dirname(__file__), "..", "agent", "tools.py")
        with open(tools_path) as f:
            source = f.read()
        # Allow the string to appear only in test files or comments about deletion
        assert "read_file_lines" not in source, (
            "read_file_lines found in tools.py — must be completely removed"
        )

    def test_not_in_config_source(self):
        config_path = os.path.join(os.path.dirname(__file__), "..", "agent", "config.py")
        with open(config_path) as f:
            source = f.read()
        assert "read_file_lines" not in source


# ---------------------------------------------------------------------------
# 8. Stash tools roundtrip
# ---------------------------------------------------------------------------

class TestStashRoundtrip:
    def test_stash_list_pop(self, tmp_path):
        from app.agent.tools import write_git_stash, read_git_stash_list, write_git_stash_pop

        # Set up a real git repo with a tracked file
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
        f = tmp_path / "file.py"
        f.write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)

        # Make a tracked change
        f.write_text("x = 2\n")

        token = _task_git_cwd.set(str(tmp_path))
        try:
            stash_result = write_git_stash("test-stash-message")
            assert "ERROR" not in stash_result

            list_result = read_git_stash_list()
            assert "test-stash-message" in list_result or "stash@{0}" in list_result

            pop_result = write_git_stash_pop()
            assert "ERROR" not in pop_result

            # File should be back to x = 2
            assert f.read_text() == "x = 2\n"
        finally:
            _task_git_cwd.reset(token)


# ---------------------------------------------------------------------------
# 9. find_in_files head/tail/grep params
# ---------------------------------------------------------------------------

class TestFindInFilesSlicing:
    def test_head_limits_results(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("\n".join(f"match_{i}" for i in range(20)))
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = find_in_files("match_", str(tmp_path), head=5)
            lines = [ln for ln in result.splitlines() if "match_" in ln]
            assert len(lines) == 5
        finally:
            _task_git_cwd.reset(token)

    def test_grep_filters_output(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("apple_1\nbanana_1\napple_2\nbanana_2\n")
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = find_in_files(".", str(tmp_path), grep="apple")
            assert "apple" in result
            assert "banana" not in result
        finally:
            _task_git_cwd.reset(token)

    def test_tail_returns_last_n(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("\n".join(f"item_{i}" for i in range(10)))
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = find_in_files("item_", str(tmp_path), tail=3)
            lines = [ln for ln in result.splitlines() if "item_" in ln]
            assert len(lines) == 3
            # Last 3 items (7, 8, 9)
            assert all(any(f"item_{i}" in ln for ln in lines) for i in [7, 8, 9])
        finally:
            _task_git_cwd.reset(token)


# ---------------------------------------------------------------------------
# 10. run_test_pytest head/tail/grep params (patched shell)
# ---------------------------------------------------------------------------

class TestRunTestPytestSlicing:
    def test_head_slices_output(self, tmp_path, monkeypatch):
        from app.agent import tools as tools_mod

        fake_output = "\n".join(f"line_{i}" for i in range(20))

        def fake_execute(cmd, cwd, timeout, timeout_msg, *, replace_python=False):
            return fake_output

        monkeypatch.setattr(tools_mod, "_execute_in_project", fake_execute)
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = tools_mod.run_test_pytest(head=5)
            lines = result.strip().splitlines()
            assert len(lines) == 5
            assert lines[0] == "line_0"
        finally:
            _task_git_cwd.reset(token)

    def test_grep_filters_output(self, tmp_path, monkeypatch):
        from app.agent import tools as tools_mod

        fake_output = "PASSED test_foo\nFAILED test_bar\nPASSED test_baz"

        def fake_execute(cmd, cwd, timeout, timeout_msg, *, replace_python=False):
            return fake_output

        monkeypatch.setattr(tools_mod, "_execute_in_project", fake_execute)
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = tools_mod.run_test_pytest(grep="FAILED")
            assert "FAILED" in result
            assert "PASSED" not in result
        finally:
            _task_git_cwd.reset(token)
